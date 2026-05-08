"""
Gateway MCP клиент (WP-209 Ф0).

Единый клиент для работы с Gateway MCP (mcp.aisystant.com/mcp).
Заменяет прямые подключения к knowledge-mcp и digital-twin-mcp.

Gateway проксирует к трём бэкендам с tool prefix:
- knowledge_* → knowledge-mcp (L2: Pack, guides, DS)
- dt_* → digital-twin-mcp (ЦД пользователя)
- personal_* → personal-knowledge-mcp (L4: личные знания)

Auth: Ory Bearer token per-user. Gateway валидирует token и
определяет userId для RLS.

Использование:
    from clients.gateway_mcp import gateway_mcp

    # Поиск по всем бэкендам (unified)
    results = await gateway_mcp.search("системное мышление", telegram_user_id=123)

    # Поиск по знаниям (L2)
    results = await gateway_mcp.knowledge_search("что такое Pack", telegram_user_id=123)

    # Чтение ЦД
    data = await gateway_mcp.dt_read("1_declarative", telegram_user_id=123)

    # Поиск по личным знаниям (L4)
    results = await gateway_mcp.personal_search("мои заметки", telegram_user_id=123)
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp

from config import GATEWAY_MCP_URL, get_logger

logger = get_logger(__name__)


class GatewayMCPClient:
    """Клиент для Gateway MCP с per-user Ory auth.

    Совмещает:
    - JSON-RPC вызовы через Gateway (tool prefix routing)
    - Per-user Ory token management (load/refresh/store)
    - Circuit breaker для graceful degradation
    """

    DEFAULT_TIMEOUT = 10
    RETRY_TIMEOUT = 5
    MAX_RETRIES = 1

    FAILURE_THRESHOLD = 2
    RECOVERY_TIME = 60

    def __init__(self, url: str):
        self.url = url
        self._request_id = 0

        # Per-user tokens: telegram_user_id -> {access_token, refresh_token, expires_at, ory_id}
        self._tokens: Dict[int, Dict[str, Any]] = {}

        # WP-209 Ф5: per-user locks для refresh. Устраняет thundering herd:
        # при параллельных запросах от одного user все получают 401, каждый
        # вызывает refresh, Ory rotation инвалидирует старый refresh_token для
        # всех кроме первого → invalid_grant → disconnect. Lock сериализует:
        # первый рефрешит, остальные ждут и видят свежий token в double-check.
        self._refresh_locks: Dict[int, asyncio.Lock] = {}

        # WP-209 Ф4: счётчик Cloudflare rate limit events. Читается из
        # dev-команды /errors для мониторинга 429.
        self._rate_limited_total: int = 0

        # WP-209 Ф4: global semaphore — ограничивает общее число одновременных
        # запросов к Gateway. Источник 64 × 429 в сутки — burst от scheduler
        # pre_generate_upcoming (Semaphore 20 × N MCP-вызовов на user → 40-60
        # параллельных запросов в Gateway). Cloudflare WAF срабатывает на
        # burst. 12 concurrent — компромисс: пропускает user-facing запросы,
        # сглаживает scheduler fan-out.
        self._call_semaphore = asyncio.Semaphore(12)

        # Circuit breaker
        self._failures = 0
        self._last_failure = 0.0
        self._circuit_open = False

        # Singleton session
        self._session: Optional[aiohttp.ClientSession] = None

        # Last error code from _call() — consumed by callers to show precise UX
        # Values: "ok" | "token_expired" | "no_subscription"
        #         | "http_error" | "timeout" | "circuit_open" | "rpc_error" | "not_authorized"
        #         | "rate_limited"
        # Note: "disconnected" was an early draft value — never set in code, removed.
        self._last_call_error: str = "ok"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # =========================================================================
    # TOKEN MANAGEMENT
    # =========================================================================

    async def load_tokens_from_db(self):
        """Загрузить все Ory tokens из БД при старте бота."""
        from db.queries.ory_tokens import load_all_ory_tokens
        rows = await load_all_ory_tokens()
        for row in rows:
            expires_at = row["expires_at"]
            # Neon secrets DB возвращает TIMESTAMPTZ как aware datetime;
            # весь код сравнивает с datetime.utcnow() (naive) — нормализуем.
            if isinstance(expires_at, datetime) and expires_at.tzinfo is not None:
                expires_at = expires_at.replace(tzinfo=None)
            self._tokens[row["chat_id"]] = {
                "access_token": row["access_token"],
                "refresh_token": row["refresh_token"],
                "expires_at": expires_at,
                "ory_id": row.get("ory_id"),
            }
        logger.info(f"Gateway: loaded {len(rows)} Ory tokens from DB")

    def set_tokens(self, telegram_user_id: int, access_token: str,
                   refresh_token: str, expires_at: datetime,
                   ory_id: Optional[str] = None):
        """Установить tokens в memory (вызывается из OAuth callback)."""
        self._tokens[telegram_user_id] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "ory_id": ory_id,
        }

    def is_connected(self, telegram_user_id: int) -> bool:
        """Проверить, есть ли Ory tokens для пользователя."""
        return telegram_user_id in self._tokens

    def _get_access_token(self, telegram_user_id: int) -> Optional[str]:
        """Получить access_token для пользователя (None если нет)."""
        data = self._tokens.get(telegram_user_id)
        if not data:
            return None
        return data["access_token"]

    async def refresh_expiring_tokens(self, margin_seconds: int = 600):
        """Proactive refresh — обновляет токены, истекающие в ближайшие margin_seconds.

        Вызывается из scheduler каждые 10 мин. Делегирует в _refresh_single_token,
        который сериализован через per-user lock — устраняет race с reactive путём
        (WP-209 Ф5).
        """
        now = datetime.utcnow()
        refreshed = 0
        failed = 0

        # Snapshot кандидатов перед итерацией — self._tokens может меняться
        candidates: List[int] = []
        for user_id, data in list(self._tokens.items()):
            expires_at = data.get("expires_at")
            if not isinstance(expires_at, datetime):
                continue
            if expires_at.tzinfo is not None:
                expires_at = expires_at.replace(tzinfo=None)
            if expires_at > now + timedelta(seconds=margin_seconds):
                continue  # не истекает скоро
            if not data.get("refresh_token"):
                continue
            candidates.append(user_id)

        for user_id in candidates:
            ok = await self._refresh_single_token(user_id)
            if ok:
                refreshed += 1
            else:
                failed += 1
                # Proactive видит явный invalid_grant → refresh_token действительно
                # невалиден (в отличие от reactive race), можно безопасно удалить.
                logger.warning(f"Gateway: proactive refresh failed for user {user_id}, removing tokens")
                self._tokens.pop(user_id, None)
                self._refresh_locks.pop(user_id, None)

        if refreshed or failed:
            logger.info(f"Gateway: proactive refresh — {refreshed} ok, {failed} failed")

    async def _refresh_single_token(self, telegram_user_id: int) -> bool:
        """Refresh Ory token для одного пользователя (при 401). Возвращает True при успехе.

        Сериализован через per-user asyncio.Lock. При thundering herd (N параллельных
        вызовов из одной корутины) только первый делает реальный POST в Ory, остальные
        ждут lock и попадают в double-check ветку со свежим токеном.
        """
        lock = self._refresh_locks.setdefault(telegram_user_id, asyncio.Lock())
        async with lock:
            data = self._tokens.get(telegram_user_id)
            if not data:
                return False

            # Double-check: пока ждали lock, другая корутина могла уже обновить
            expires_at = data.get("expires_at")
            if isinstance(expires_at, datetime) and expires_at.tzinfo is not None:
                expires_at = expires_at.replace(tzinfo=None)
            if isinstance(expires_at, datetime) and expires_at > datetime.utcnow() + timedelta(seconds=60):
                logger.debug(f"Gateway: refresh skipped for user {telegram_user_id} — token already fresh")
                return True

            refresh_token = data.get("refresh_token")
            if not refresh_token:
                return False
            try:
                from clients.ory_oauth import ory_oauth
                from db.queries.ory_tokens import save_ory_tokens

                new_tokens = await ory_oauth.refresh_access_token(refresh_token)
                if new_tokens:
                    new_expires_at = datetime.utcnow() + timedelta(
                        seconds=new_tokens.get("expires_in", 3600)
                    )
                    new_access = new_tokens["access_token"]
                    new_refresh = new_tokens.get("refresh_token", refresh_token)

                    self._tokens[telegram_user_id] = {
                        "access_token": new_access,
                        "refresh_token": new_refresh,
                        "expires_at": new_expires_at,
                        "ory_id": data.get("ory_id"),
                    }
                    await save_ory_tokens(
                        chat_id=telegram_user_id,
                        access_token=new_access,
                        refresh_token=new_refresh,
                        expires_at=new_expires_at,
                        ory_id=data.get("ory_id"),
                    )
                    logger.info(f"Gateway: refreshed token for user {telegram_user_id}")
                    return True
                # B4.20 диагностика: ory_oauth вернул None (детали выше в [OryOAuth] логе)
                logger.warning(
                    f"Gateway: refresh returned None for user {telegram_user_id} "
                    f"(ory_id={data.get('ory_id', '?')[:8]}..., "
                    f"token_age={(datetime.utcnow() - expires_at).seconds // 60 if isinstance(expires_at, datetime) else '?'}min expired)"
                )
                return False
            except Exception as e:
                logger.error(f"Gateway: refresh error for user {telegram_user_id}: {e}", exc_info=True)
                return False

    # =========================================================================
    # CIRCUIT BREAKER
    # =========================================================================

    def _is_circuit_open(self) -> bool:
        if not self._circuit_open:
            return False
        if time.time() - self._last_failure > self.RECOVERY_TIME:
            logger.info("Gateway: circuit breaker half-open, trying recovery")
            return False
        return True

    def _record_failure(self):
        self._failures += 1
        self._last_failure = time.time()
        if self._failures >= self.FAILURE_THRESHOLD:
            self._circuit_open = True
            logger.warning("Gateway: circuit breaker OPEN")

    def _record_success(self):
        if self._failures > 0 or self._circuit_open:
            logger.info("Gateway: circuit breaker CLOSED")
        self._failures = 0
        self._circuit_open = False

    # =========================================================================
    # JSON-RPC CALLS
    # =========================================================================

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _call(self, tool_name: str, arguments: dict,
                    telegram_user_id: Optional[int] = None) -> Optional[dict]:
        """Вызов инструмента Gateway MCP через JSON-RPC.

        Args:
            tool_name: имя инструмента (с prefix, напр. 'knowledge_search')
            arguments: аргументы вызова
            telegram_user_id: ID пользователя для Bearer token

        Returns:
            Результат вызова или None при ошибке
        """
        self._last_call_error = "ok"

        if self._is_circuit_open():
            logger.debug("Gateway: circuit breaker open, skipping")
            self._last_call_error = "circuit_open"
            return None

        # WP-209 Ф4: сериализуем запросы к Gateway через global semaphore.
        # Защита от Cloudflare 429 (burst от scheduler pre-gen fan-out).
        async with self._call_semaphore:
            return await self._do_call(tool_name, arguments, telegram_user_id)

    async def _do_call(self, tool_name: str, arguments: dict,
                       telegram_user_id: Optional[int] = None) -> Optional[dict]:
        """Фактический HTTP call. Вызывается только из _call под semaphore."""

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments
            },
            "id": self._next_id()
        }

        # Auth header
        headers: Dict[str, str] = {}
        if telegram_user_id:
            token = self._get_access_token(telegram_user_id)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            else:
                # Требуется пользовательский контекст, но токенов нет
                self._last_call_error = "not_authorized"

        # Trace correlation
        try:
            from core.tracing import get_current_trace
            trace = get_current_trace()
            if trace:
                headers["x-trace-id"] = trace.trace_id
        except ImportError:
            pass

        logger.debug(f"Gateway: {tool_name}({arguments}), user={telegram_user_id}")

        session = await self._get_session()
        last_error = None

        for attempt in range(self.MAX_RETRIES + 1):
            timeout = self.DEFAULT_TIMEOUT if attempt == 0 else self.RETRY_TIMEOUT

            try:
                async with session.post(
                    self.url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status == 401 and telegram_user_id and attempt == 0:
                        # Token expired — try refresh
                        logger.info(f"Gateway: 401 for user {telegram_user_id}, attempting refresh")
                        refreshed = await self._refresh_single_token(telegram_user_id)
                        if refreshed:
                            # Update header and retry
                            new_token = self._get_access_token(telegram_user_id)
                            if new_token:
                                headers["Authorization"] = f"Bearer {new_token}"
                            continue
                        else:
                            # WP-209 Ф5: не disconnect() в reactive пути. Raced
                            # thundering herd мог успешно обновить токен в другой
                            # корутине — disconnect всё сносит. Помечаем как
                            # token_expired, UX показывает кнопку переподключения.
                            # Окончательный disconnect — только в proactive cron
                            # после явного invalid_grant от Ory.
                            logger.warning(f"Gateway: refresh failed for user {telegram_user_id} (not disconnecting)")
                            self._last_call_error = "token_expired"
                            return None
                    elif resp.status == 401:
                        logger.warning(f"Gateway: 401 for user {telegram_user_id} after retry")
                        self._last_call_error = "token_expired"
                        return None
                    if resp.status == 403:
                        logger.warning(f"Gateway: 403 for user {telegram_user_id} — no subscription")
                        self._last_call_error = "no_subscription"
                        return None
                    if resp.status == 429 and attempt == 0:
                        # WP-209 Ф4: Cloudflare rate limit. Парсим Retry-After,
                        # ждём, повторяем (max 1 попытка). Источник бёрстов —
                        # scheduler-jobs (pre-gen, digest fan-out) из-за WP-212
                        # B4.13 (все knowledge-запросы через Gateway).
                        retry_after_raw = resp.headers.get("Retry-After", "1")
                        try:
                            retry_after = min(float(retry_after_raw), 5.0)
                        except (ValueError, TypeError):
                            retry_after = 1.0
                        logger.warning(
                            f"Gateway: 429 rate limited, Retry-After={retry_after_raw} "
                            f"→ sleeping {retry_after:.1f}s (user={telegram_user_id})"
                        )
                        self._rate_limited_total += 1
                        await asyncio.sleep(retry_after)
                        continue
                    elif resp.status == 429:
                        logger.warning(f"Gateway: 429 after retry (user={telegram_user_id})")
                        self._last_call_error = "rate_limited"
                        self._record_failure()
                        return None
                    if resp.status == 200:
                        data = await resp.json()
                        if "result" in data:
                            self._record_success()
                            return data["result"]
                        if "error" in data:
                            error_msg = data["error"].get("message", str(data["error"]))
                            logger.error(f"Gateway JSON-RPC error: {error_msg}")
                            self._last_call_error = "rpc_error"
                            return None
                        logger.warning(f"Gateway: unexpected response: {list(data.keys())}")
                        self._last_call_error = "rpc_error"
                        return None
                    else:
                        error = await resp.text()
                        logger.error(f"Gateway HTTP {resp.status}: {error[:200]}")
                        last_error = f"HTTP {resp.status}"
                        self._last_call_error = "http_error"
                        self._record_failure()
            except asyncio.TimeoutError:
                last_error = "timeout"
                self._last_call_error = "timeout"
                if attempt < self.MAX_RETRIES:
                    logger.warning(f"Gateway timeout ({timeout}s), retry {attempt + 1}")
                    await asyncio.sleep(1)
                    continue
                self._record_failure()
            except Exception as e:
                logger.error(f"Gateway exception: {e}")
                self._last_call_error = "http_error"
                self._record_failure()
                return None

        logger.warning("Gateway: unavailable, continuing without MCP")
        return None

    def _parse_text_content(self, result: Optional[dict]) -> Any:
        """Извлекает текстовый контент из MCP result.

        Корректный MCP spec: если tool вернул ошибку execution, result имеет
        isError=true. Gateway проксирует backend-ошибки именно так, content
        содержит "Backend error: ..." как текстовое описание. Клиент обязан
        читать isError и не трактовать text как успех.
        """
        if not result or "content" not in result:
            return None
        # MCP spec: tool execution error indicator
        if result.get("isError"):
            error_text = ""
            for item in result.get("content", []):
                if item.get("type") == "text":
                    error_text = item.get("text", "")
                    break
            logger.warning(f"Gateway: MCP tool isError=true, content={error_text[:200]}")
            self._last_call_error = "rpc_error"
            return None
        for item in result.get("content", []):
            if item.get("type") == "text":
                text = item.get("text", "null")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return None

    # =========================================================================
    # UNIFIED SEARCH (fan-out по всем бэкендам)
    # =========================================================================

    async def search(self, query: str, telegram_user_id: Optional[int] = None,
                     limit: int = 5) -> List[dict]:
        """Unified search через Gateway (L2 + L4 + BYOB).

        Gateway делает fan-out ко всем бэкендам, дедуплицирует, сортирует по score.
        """
        result = await self._call("search", {"query": query, "limit": limit}, telegram_user_id)
        data = self._parse_text_content(result)
        if isinstance(data, list):
            return data
        return []

    # =========================================================================
    # KNOWLEDGE MCP (prefix: knowledge_)
    # =========================================================================

    async def knowledge_search(self, query: str, limit: int = 5,
                               source_type: Optional[str] = None,
                               telegram_user_id: Optional[int] = None) -> List[dict]:
        """Поиск по знаниям (L2: Pack, guides, DS).

        B4.20: при token_expired — fallback на анонимный запрос (без Bearer).
        knowledge-mcp (L2) возвращает платформенные документы (user_id IS NULL)
        без аутентификации. Личные документы (L4) не возвращаются — это ожидаемо.
        """
        args: Dict[str, Any] = {"query": query, "limit": limit}
        if source_type:
            args["source_type"] = source_type

        result = await self._call("knowledge_search", args, telegram_user_id)
        data = self._parse_text_content(result)
        if isinstance(data, list):
            return data

        # B4.20: fallback — токен истёк или refresh не прошёл, но L2 публичный
        if self._last_call_error == "token_expired" and telegram_user_id:
            logger.info(f"Gateway: knowledge_search fallback (no auth) for user {telegram_user_id}")
            result = await self._call("knowledge_search", args, telegram_user_id=None)
            data = self._parse_text_content(result)
            if isinstance(data, list):
                return data

        return []

    async def knowledge_get_document(self, filename: str,
                                     source: Optional[str] = None,
                                     telegram_user_id: Optional[int] = None) -> Optional[dict]:
        """Получить документ по имени файла."""
        args: Dict[str, Any] = {"filename": filename}
        if source:
            args["source"] = source
        result = await self._call("knowledge_get_document", args, telegram_user_id)
        return self._parse_text_content(result)

    async def knowledge_list_sources(self, source_type: Optional[str] = None,
                                     telegram_user_id: Optional[int] = None) -> List[dict]:
        """Список доступных баз знаний."""
        args: Dict[str, Any] = {}
        if source_type:
            args["source_type"] = source_type
        result = await self._call("knowledge_list_sources", args, telegram_user_id)
        data = self._parse_text_content(result)
        if isinstance(data, list):
            return data
        return []

    # =========================================================================
    # DIGITAL TWIN MCP (prefix: dt_)
    # =========================================================================

    async def dt_read(self, path: str, telegram_user_id: int) -> Optional[Any]:
        """Читать данные ЦД пользователя через Gateway."""
        result = await self._call("dt_read_digital_twin", {"path": path}, telegram_user_id)
        return self._parse_text_content(result)

    async def dt_write(self, path: str, data: Any, telegram_user_id: int) -> Optional[dict]:
        """Записать данные в ЦД через Gateway."""
        result = await self._call("dt_write_digital_twin", {"path": path, "data": data}, telegram_user_id)
        return self._parse_text_content(result)

    async def dt_describe(self, path: str, telegram_user_id: Optional[int] = None) -> Optional[str]:
        """Описание метамодели ЦД."""
        result = await self._call("dt_describe_by_path", {"path": path}, telegram_user_id)
        return self._parse_text_content(result)

    # =========================================================================
    # PERSONAL KNOWLEDGE MCP (prefix: personal_)
    # =========================================================================

    async def personal_search(self, query: str, telegram_user_id: int,
                              limit: int = 5) -> List[dict]:
        """Поиск по личным знаниям пользователя (L4)."""
        result = await self._call(
            "personal_search", {"query": query, "limit": limit}, telegram_user_id
        )
        data = self._parse_text_content(result)
        if isinstance(data, list):
            return data
        return []

    async def personal_write(self, source: str, path: str, content: str,
                             message: str, telegram_user_id: int) -> Optional[dict]:
        """Запись в личный репозиторий пользователя (L4)."""
        result = await self._call(
            "personal_write",
            {"source": source, "path": path, "content": content, "message": message},
            telegram_user_id,
        )
        return self._parse_text_content(result)

    # =========================================================================
    # GATEWAY-LEVEL TOOLS
    # =========================================================================

    async def get_instructions(self, telegram_user_id: Optional[int] = None) -> Optional[str]:
        """Получить IWE system instructions через Gateway."""
        result = await self._call("get_instructions", {}, telegram_user_id)
        return self._parse_text_content(result)

    # =========================================================================
    # BACKWARD COMPATIBILITY
    # =========================================================================

    async def read(self, path: str, telegram_user_id: int) -> Optional[Any]:
        """Alias for dt_read() — backward compatibility with digital_twin.read()."""
        return await self.dt_read(path, telegram_user_id)

    async def write(self, path: str, data: Any, telegram_user_id: int) -> Optional[dict]:
        """Alias for dt_write() — backward compatibility with digital_twin.write()."""
        return await self.dt_write(path, data, telegram_user_id)

    # =========================================================================
    # УДОБНЫЕ МЕТОДЫ ДЛЯ ЦД (миграция с digital_twin, WP-209 Ф2)
    # =========================================================================

    # Маппинг: поле бота → путь в ЦД (source-of-truth: DP.AISYS.014 § 4.5.1)
    PROFILE_DT_MAPPING = {
        'name': '1_declarative/1_1_profile/02_Имя',
        'occupation': '1_declarative/1_1_profile/01_Занятие',
        'interests': '1_declarative/1_2_goals/01_Интересы',
        'goals': '1_declarative/1_2_goals/09_Цели обучения',
        'role': '1_declarative/1_3_selfeval/06_Роли',
        'study_duration': '1_declarative/1_3_selfeval/11_Срок обучения',
        'current_problems': '1_declarative/1_4_context/01_Текущие проблемы',
        'desires': '1_declarative/1_4_context/02_Желания',
        'schedule_time': '1_declarative/1_4_context/05_Режим обучения',
        'feed_schedule_time': '1_declarative/1_4_context/04_Удобное время',
        'delivery_format': '1_declarative/1_5_delivery/01_Формат подачи',
        'detail_level': '1_declarative/1_5_delivery/02_Детализация',
    }

    async def get_user_profile(self, telegram_user_id: int) -> Optional[dict]:
        """Получить полный профиль пользователя из ЦД."""
        return await self.dt_read("", telegram_user_id)

    async def get_user_profile_ex(self, telegram_user_id: int) -> tuple:
        """Получить профиль ЦД + код ошибки.

        Returns:
            (profile_dict_or_None, reason)
            reason ∈ {"ok", "empty", "disconnected", "token_expired",
                      "no_subscription", "http_error", "timeout",
                      "circuit_open", "rpc_error", "not_authorized",
                      "rate_limited"}
        """
        profile = await self.dt_read("", telegram_user_id)
        reason = self._last_call_error
        if profile is None and reason == "ok":
            # Вызов успешен, но данных нет (пустой ЦД)
            reason = "empty"
        elif profile is not None and not isinstance(profile, dict):
            # Unexpected response shape (string, list, etc.) — dt-mcp server
            # вернул что-то кроме JSON-объекта профиля. Часто случается когда
            # ЦД пользователя ещё не инициализирован и сервер отдаёт текст
            # «profile not found» как content.type=text.
            logger.warning(
                f"Gateway: get_user_profile_ex got non-dict profile "
                f"(type={type(profile).__name__}) for user {telegram_user_id}; "
                f"preview={str(profile)[:200]!r}"
            )
            profile = None
            reason = "empty"
        elif profile is not None:
            reason = "ok"
        return profile, reason

    def get_connected_user_ids(self) -> List[int]:
        """Список ID пользователей с Ory tokens."""
        return list(self._tokens.keys())

    def disconnect(self, telegram_user_id: int):
        """Отключает пользователя — удаляет Ory tokens из памяти и БД."""
        if telegram_user_id in self._tokens:
            del self._tokens[telegram_user_id]
            logger.info(f"Gateway: disconnected user {telegram_user_id}")
        # Очистить per-user lock (WP-209 Ф5)
        self._refresh_locks.pop(telegram_user_id, None)
        # Удалить из DB (fire-and-forget)
        asyncio.ensure_future(self._delete_ory_tokens_from_db(telegram_user_id))

    async def _delete_ory_tokens_from_db(self, telegram_user_id: int) -> None:
        """Удалить Ory tokens из DB."""
        try:
            from db.queries.ory_tokens import delete_ory_tokens
            await delete_ory_tokens(telegram_user_id)
        except Exception as e:
            logger.error(f"Gateway: failed to delete tokens for {telegram_user_id}: {e}")

    @staticmethod
    def _convert_value(field: str, value: Any) -> Any:
        """Конвертация значения бота в формат ЦД."""
        if value is None:
            return ""
        if field == 'interests':
            if isinstance(value, list):
                return ", ".join(value)
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        return ", ".join(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
            return str(value)
        if field == 'study_duration':
            return str(value)
        return str(value) if not isinstance(value, str) else value

    async def sync_profile(self, telegram_user_id: int, intern_data: dict) -> int:
        """Полный перелив профиля бота → ЦД через Gateway. Возвращает кол-во записанных полей."""
        if not self.is_connected(telegram_user_id):
            return 0

        synced = 0
        for field, dt_path in self.PROFILE_DT_MAPPING.items():
            value = intern_data.get(field)
            if value is None or value == '' or value == '[]':
                continue
            converted = self._convert_value(field, value)
            if not converted:
                continue
            try:
                result = await self.dt_write(dt_path, converted, telegram_user_id)
                if result is not None:
                    synced += 1
            except Exception as e:
                logger.error(f"Gateway sync field {field} failed: {e}")

        logger.info(f"Gateway sync: user {telegram_user_id}, {synced}/{len(self.PROFILE_DT_MAPPING)} fields")
        return synced

    async def sync_fields(self, telegram_user_id: int, fields: dict) -> int:
        """Инкрементальный sync: только указанные поля. Возвращает кол-во записанных."""
        if not self.is_connected(telegram_user_id):
            return 0

        synced = 0
        for field, value in fields.items():
            dt_path = self.PROFILE_DT_MAPPING.get(field)
            if not dt_path:
                continue
            converted = self._convert_value(field, value)
            try:
                result = await self.dt_write(dt_path, converted, telegram_user_id)
                if result is not None:
                    synced += 1
            except Exception as e:
                logger.error(f"Gateway sync field {field} failed: {e}")
        return synced


# Singleton
gateway_mcp = GatewayMCPClient(GATEWAY_MCP_URL)
