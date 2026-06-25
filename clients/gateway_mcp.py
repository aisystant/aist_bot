from __future__ import annotations

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
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp

from config import GATEWAY_MCP_URL, GATEWAY_MCP_TIMEOUT, get_logger

logger = get_logger(__name__)


class GatewayMCPClient:
    """Клиент для Gateway MCP с per-user Ory auth.

    Совмещает:
    - JSON-RPC вызовы через Gateway (tool prefix routing)
    - Per-user Ory token management (load/refresh/store)
    - Circuit breaker для graceful degradation
    """

    DEFAULT_TIMEOUT = GATEWAY_MCP_TIMEOUT
    RETRY_TIMEOUT = GATEWAY_MCP_TIMEOUT
    MAX_RETRIES = 1

    FAILURE_THRESHOLD = 2
    RECOVERY_TIME = 60

    TOOLS_CACHE_TTL = 15 * 60  # 15 min (DP.SC.129)

    def __init__(self, url: str):
        self.url = url
        self._request_id = 0

        # Per-user tokens: telegram_user_id -> {access_token, refresh_token, expires_at, ory_id}
        self._tokens: Dict[int, Dict[str, Any]] = {}

        # DP.SC.129: discovered tools cache (in-memory, single-instance)
        self._tools_cache: List[Dict] = []
        self._tools_cache_ts: float = 0.0

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
                # WP-200 follow-up: НЕ удаляем токен здесь. _refresh_single_token
                # сам делает pop+delete_ory_tokens только при InvalidGrantError.
                # На сетевые ошибки/таймауты/5xx — токен остаётся, retry на следующем
                # cycle (10 мин) или reactive path (401 → refresh). Иначе transient
                # сбой Ory мгновенно отключает пользователя без реального revoke.
                logger.warning(
                    f"Gateway: proactive refresh failed for user {user_id} "
                    f"(state managed by _refresh_single_token)"
                )

        if refreshed or failed:
            logger.info(f"Gateway: proactive refresh — {refreshed} ok, {failed} failed")

    async def _refresh_single_token(self, telegram_user_id: int) -> bool:
        """Refresh Ory token для одного пользователя (при 401). Возвращает True при успехе.

        Два слоя сериализации:
          - in-memory `_refresh_locks[user_id]` — concurrent корутины внутри **одного** процесса.
          - БД `SELECT ... FOR UPDATE` через `refresh_ory_token_with_lock` — concurrent **процессы**
            (бот в Railway + iwe-gateway-bridge.py локально). WP-200 follow-up — устраняет
            multi-writer race на rotating refresh_token Ory.

        Layered порядок: asyncio.Lock → asyncpg transaction → FOR UPDATE.
        Deadlock невозможен — порядок захвата детерминирован (per-user).
        """
        lock = self._refresh_locks.setdefault(telegram_user_id, asyncio.Lock())
        async with lock:
            data = self._tokens.get(telegram_user_id)
            if not data:
                # Cache miss — закрывает silent Путь A (peer-session 2026-06-01-19,
                # Block GTW): 6+ user_id за 7 дней получали `refresh failed (not
                # disconnecting)` без предшествующих refresh-логов = единственный
                # путь возврата False БЕЗ логирования. Подгипотезы A1 (OAuth не
                # завершился), A2 (restart wiped memory + load_tokens_from_db
                # пропустил), A3 (save silently failed), A4 multi-worker — все
                # закрываются единым defensive reload из БД + явный лог.
                logger.warning(
                    f"Gateway: token cache miss for user {telegram_user_id} — "
                    f"attempting DB reload"
                )
                from db.queries.ory_tokens import load_one_ory_token
                row = await load_one_ory_token(telegram_user_id)
                if row is None:
                    logger.warning(
                        f"Gateway: no token for user {telegram_user_id} in cache or DB "
                        f"— treating as not connected (re-auth needed)"
                    )
                    return False
                # Repopulate in-memory cache и идём в нормальный refresh-путь —
                # `refresh_ory_token_with_lock` сделает double-check expires_at
                # и вернёт cached или запросит Ory.
                expires_at = row["expires_at"]
                if isinstance(expires_at, datetime) and expires_at.tzinfo is not None:
                    expires_at = expires_at.replace(tzinfo=None)
                self._tokens[telegram_user_id] = {
                    "access_token": row["access_token"],
                    "refresh_token": row["refresh_token"],
                    "expires_at": expires_at,
                    "ory_id": row.get("ory_id"),
                }
                logger.info(
                    f"Gateway: token reloaded from DB for user {telegram_user_id}"
                )
                data = self._tokens[telegram_user_id]

            # NB: In-memory double-check НЕ делаем — _tokens может быть несвежим
            # относительно БД (bridge мог обновить под FOR UPDATE'ом). Пусть
            # refresh_ory_token_with_lock сам сделает double-check под БД-lock'ом —
            # round-trip ~10ms против риска вернуть stale access_token.
            expires_at = data.get("expires_at")
            if isinstance(expires_at, datetime) and expires_at.tzinfo is not None:
                expires_at = expires_at.replace(tzinfo=None)

            try:
                from clients.ory_oauth import ory_oauth, InvalidGrantError
                from db.queries.ory_tokens import refresh_ory_token_with_lock, delete_ory_tokens

                # БД-сериализация: refresh выполняется под row-lock.
                # Если другой процесс (bridge) уже сделал refresh → double-check внутри
                # `refresh_ory_token_with_lock` вернёт свежий токен из БД без вызова Ory.
                result = await refresh_ory_token_with_lock(
                    chat_id=telegram_user_id,
                    refresh_fn=ory_oauth.refresh_access_token,
                    fresh_threshold_seconds=60,
                )
                if result is None:
                    logger.warning(
                        f"Gateway: refresh returned None for user {telegram_user_id} "
                        f"(ory_id={(data.get('ory_id') or '?')[:8]}..., "
                        f"token_age={(datetime.utcnow() - expires_at).seconds // 60 if isinstance(expires_at, datetime) else '?'}min expired)"
                    )
                    return False

                # Sync in-memory cache с тем, что в БД (могло измениться bridge'ем).
                self._tokens[telegram_user_id] = {
                    "access_token": result["access_token"],
                    "refresh_token": result["refresh_token"],
                    "expires_at": result["expires_at"],
                    "ory_id": result["ory_id"] or data.get("ory_id"),
                }
                if result.get("from_cache"):
                    logger.info(
                        f"Gateway: token for user {telegram_user_id} already fresh in DB "
                        f"(updated by another writer, e.g. bridge)"
                    )
                else:
                    logger.info(f"Gateway: refreshed token for user {telegram_user_id}")
                return True
            except InvalidGrantError:
                # Refresh token отозван (Ory вернул invalid_grant) — транзакция в
                # refresh_ory_token_with_lock уже откатилась (raise внутри async with
                # transaction = rollback). Чистим in-memory и БД отдельно.
                self._tokens.pop(telegram_user_id, None)
                try:
                    await delete_ory_tokens(telegram_user_id)
                except Exception:
                    pass
                logger.warning(f"Gateway: invalid_grant for user {telegram_user_id} — token cleared, re-auth required")
                return False
            except Exception as e:
                logger.error(f"Gateway: refresh error for user {telegram_user_id}: {e}", exc_info=True)
                return False
        # NB: _refresh_locks НЕ удаляется при invalid_grant — lock остаётся в setdefault-словаре
        # до restart процесса. Это benign: при re-auth user_id получит свежий setdefault.
        # Удалять lock под `async with lock` самим owner'ом — race с параллельной setdefault.

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

    # WP-392 Ф3.1: hermes_chat — прокси к Hermes-рантайму (DP.SC.167, T4)
    HERMES_TIMEOUT_MESSAGE = (
        "Запрос обрабатывался слишком долго (лимит 25 секунд). "
        "Попробуй упростить вопрос или разбить на части."
    )
    # WP-392: hermes_chat требует отдельного таймаута — Railway Hermes может работать до 60с
    # (ASCII-арт, HTML, диаграммы). Глобальный GATEWAY_MCP_TIMEOUT=35 — временная заглушка
    # в Railway env; здесь — постоянный per-tool override.
    HERMES_CALL_TIMEOUT = int(os.getenv("GATEWAY_MCP_HERMES_TIMEOUT", "60"))

    async def hermes_chat(
        self,
        message: str,
        telegram_user_id: int,
        session_id: Optional[str] = None,
    ) -> Optional[str]:
        """Вызов hermes_chat через gateway-mcp.

        Возвращает текст ответа (или информативное сообщение о таймауте — его
        показываем пользователю), либо None если рантайм недоступен. На None
        вызывающий сам решает, что показать: переподключение (нет токена) или
        нейтральное «попробуй позже». Имя рантайма наружу не утекает (РП7
        BOT-HERMES1).
        """
        args: dict = {"message": message}
        if session_id:
            args["session_id"] = session_id
        result = await self._call(
            "hermes_chat", args,
            telegram_user_id=telegram_user_id,
            timeout=self.HERMES_CALL_TIMEOUT,
        )
        if result is None:
            return None
        # Gateway возвращает { content: [{ type: "text", text: "..." }], isError?: bool }
        content = result.get("content", [])
        raw_text = content[0].get("text", "") if content else ""
        # Парсим JSON-ответ gateway (timeout / error / нормальный)
        try:
            import json
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                if parsed.get("error") == "timeout":
                    return parsed.get("message", self.HERMES_TIMEOUT_MESSAGE)
                if parsed.get("error"):
                    return None
                if "response" in parsed:
                    return str(parsed["response"])
        except (ValueError, TypeError):
            pass
        return raw_text or None

    async def _call(self, tool_name: str, arguments: dict,
                    telegram_user_id: Optional[int] = None,
                    timeout: Optional[int] = None) -> Optional[dict]:
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
            return await self._do_call(tool_name, arguments, telegram_user_id, timeout=timeout)

    async def _do_call(self, tool_name: str, arguments: dict,
                       telegram_user_id: Optional[int] = None,
                       timeout: Optional[int] = None) -> Optional[dict]:
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
                # Требуется пользовательский контекст, но токенов нет.
                # Не делаем запрос без авторизации — gateway отобьёт 401 ещё до
                # маршрутизации, а refresh нечего рефрешить.
                self._last_call_error = "not_authorized"
                logger.warning(
                    f"Gateway: call to '{tool_name}' for user {telegram_user_id} "
                    f"skipped — no Ory token (re-auth needed)"
                )
                return None

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
            timeout = (timeout or self.DEFAULT_TIMEOUT) if attempt == 0 else (timeout or self.RETRY_TIMEOUT)

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

    async def capture_trace(self, sensor_id: str, event_type: str, content: dict,
                            telegram_user_id: int,
                            external_id: Optional[str] = None) -> Optional[dict]:
        """Record a user trace via the gateway -> trace-accountant (WP-427, DP.SC.182).

        Additive-consent sensors (bot_note, bot_reflection) write straight to domain_event.
        The user is identified by the per-user Ory token the gateway forwards (the backend
        reads `sub` from the gateway assertion), so no user_id is sent from here.
        external_id lets retries of the same note dedup on the backend (ON CONFLICT).
        """
        args = {"sensor_id": sensor_id, "event_type": event_type, "content": content}
        if external_id:
            args["external_id"] = external_id
        return await self._call("capture_trace", args, telegram_user_id)

    # =========================================================================
    # GATEWAY-LEVEL TOOLS
    # =========================================================================

    async def get_instructions(self, telegram_user_id: Optional[int] = None) -> Optional[str]:
        """Получить IWE system instructions через Gateway."""
        result = await self._call("get_instructions", {}, telegram_user_id)
        return self._parse_text_content(result)

    # =========================================================================
    # WP-349 Ф30: ONBOARDING PROJECTION TOOLS
    # =========================================================================

    async def get_journey_state(self, telegram_user_id: int) -> Optional[dict]:
        """Получить состояние пути оснащения пилота через Gateway.

        Returns:
            dict с ключами tech_tier, tier_source, content_stage, github_connected, has_consent
            или None при ошибке.
        """
        result = await self._call("get_journey_state", {}, telegram_user_id=telegram_user_id)
        return self._parse_text_content(result)

    async def get_next_onboarding_step(self, telegram_user_id: int) -> Optional[dict]:
        """Получить следующий приоритетный шаг онбординга через Gateway.

        Returns:
            dict с ключами journey_state, next_step
            или None при ошибке.
        """
        result = await self._call("get_next_onboarding_step", {}, telegram_user_id=telegram_user_id)
        return self._parse_text_content(result)

    async def grant_consent(
        self,
        telegram_user_id: int,
        agreed: bool = True,
        scopes: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """Записать согласие на обработку данных через Gateway.

        Args:
            agreed: True — дать согласие, False — отозвать
            scopes: список scope (default: ["data_analysis", "text_analysis"])

        Returns:
            dict с ключами success, scopes_granted
            или None при ошибке.
        """
        args: dict = {"agreed": agreed}
        if scopes is not None:
            args["scopes"] = scopes
        result = await self._call("grant_consent", args, telegram_user_id=telegram_user_id)
        return self._parse_text_content(result)

    async def request_equipment_upgrade(
        self,
        telegram_user_id: int,
        target_tier: str,
        channel: str = "telegram",
        trigger: Optional[str] = None,
    ) -> Optional[dict]:
        """Запросить апгрейд оборудования через Gateway.

        Args:
            target_tier: "T2", "T3" или "T4"
            channel: канал запроса (telegram, web, vscode)
            trigger: что спровоцировало запрос (nudge_f, nudge_g, user_action)

        Returns:
            dict с ключами success, target_tier, current_tier, next_steps, requirements
            или None при ошибке.
        """
        args: dict = {"target_tier": target_tier, "channel": channel}
        if trigger:
            args["trigger"] = trigger
        result = await self._call("request_equipment_upgrade", args, telegram_user_id=telegram_user_id)
        return self._parse_text_content(result)

    async def get_onboarding_context(self, telegram_user_id: int) -> Optional[dict]:
        """Получить полный онбординг-контекст пилота через Gateway.

        Returns:
            dict с JourneyState + история шагов + активные consent + program_hint
            или None при ошибке.
        """
        result = await self._call("get_onboarding_context", {}, telegram_user_id=telegram_user_id)
        return self._parse_text_content(result)

    # =========================================================================
    # WP-349 Ф29: BYOK — управление пользовательскими LLM-ключами (T4)
    # =========================================================================

    async def list_llm_keys(self, telegram_user_id: int) -> Optional[list]:
        """Список BYOK LLM-ключей пользователя (маскированные)."""
        result = await self._call("list_llm_keys", {}, telegram_user_id=telegram_user_id)
        parsed = self._parse_text_content(result)
        if isinstance(parsed, dict):
            return parsed.get("keys", [])
        return None

    async def grant_llm_key(
        self,
        telegram_user_id: int,
        provider: str,
        api_key: str,
        label: Optional[str] = None,
        is_default: bool = False,
    ) -> Optional[dict]:
        """Сохранить LLM API-ключ (зашифровывается в gateway)."""
        args: dict = {"provider": provider, "api_key": api_key, "is_default": is_default}
        if label:
            args["label"] = label
        result = await self._call("grant_llm_key", args, telegram_user_id=telegram_user_id)
        return self._parse_text_content(result)

    async def revoke_llm_key(self, telegram_user_id: int, key_id: str) -> Optional[dict]:
        """Отозвать LLM API-ключ по UUID."""
        result = await self._call("revoke_llm_key", {"key_id": key_id}, telegram_user_id=telegram_user_id)
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


    # =========================================================================
    # TOOL DISCOVERY (DP.SC.129, DP.ROLE.038)
    # =========================================================================

    async def list_tools(self) -> List[Dict]:
        """Загрузить список tool из Gateway MCP (tools/list) и обновить кэш.

        Вызывается при старте бота и как background refresh (TTL 15 мин).
        При ошибке возвращает stale-кэш (или пустой список при cold start).
        Gateway требует Ory Bearer даже для discovery — используем любой
        из загруженных токенов (результат не зависит от конкретного user).
        """
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {},
            "id": self._next_id()
        }
        # Use any available user token — gateway requires auth even for discovery.
        # Tokens are loaded from DB before list_tools() is called at startup.
        discovery_headers: Dict[str, str] = {}
        if self._tokens:
            any_token = next(iter(self._tokens.values()))["access_token"]
            discovery_headers["Authorization"] = f"Bearer {any_token}"

        session = await self._get_session()
        try:
            async with self._call_semaphore:
                async with session.post(
                    self.url,
                    json=payload,
                    headers=discovery_headers,
                    timeout=aiohttp.ClientTimeout(total=self.DEFAULT_TIMEOUT)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        mcp_tools = data.get("result", {}).get("tools", [])
                        tools = [self._mcp_to_anthropic_tool(t) for t in mcp_tools]
                        self._tools_cache = tools
                        self._tools_cache_ts = time.time()
                        logger.info(f"Gateway: discovery loaded {len(tools)} tools")
                        return tools
                    logger.warning(f"Gateway: tools/list HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"Gateway: list_tools failed: {e}")
        return list(self._tools_cache)

    def _mcp_to_anthropic_tool(self, mcp_tool: Dict) -> Dict:
        """MCP tool format → Anthropic API format (inputSchema → input_schema)."""
        return {
            "name": mcp_tool.get("name", ""),
            "description": mcp_tool.get("description", ""),
            "input_schema": mcp_tool.get("inputSchema", {
                "type": "object",
                "properties": {},
                "required": []
            })
        }

    def get_discovered_tools(self) -> List[Dict]:
        """Список инструментов из последнего tools/list (может быть stale)."""
        return list(self._tools_cache)

    def is_tools_cache_fresh(self) -> bool:
        """True если кэш tool свежее TTL."""
        return (time.time() - self._tools_cache_ts) < self.TOOLS_CACHE_TTL


# Singleton
gateway_mcp = GatewayMCPClient(GATEWAY_MCP_URL)
