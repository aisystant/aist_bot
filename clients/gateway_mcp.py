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

        # Circuit breaker
        self._failures = 0
        self._last_failure = 0.0
        self._circuit_open = False

        # Singleton session
        self._session: Optional[aiohttp.ClientSession] = None

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
            self._tokens[row["chat_id"]] = {
                "access_token": row["access_token"],
                "refresh_token": row["refresh_token"],
                "expires_at": row["expires_at"],
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

        Вызывается из scheduler каждые 10 мин.
        """
        from clients.ory_oauth import ory_oauth
        from db.queries.ory_tokens import save_ory_tokens

        now = datetime.utcnow()
        refreshed = 0
        failed = 0

        for user_id, data in list(self._tokens.items()):
            expires_at = data.get("expires_at")
            if not expires_at:
                continue

            # naive datetime comparison
            if isinstance(expires_at, datetime) and expires_at > now + timedelta(seconds=margin_seconds):
                continue  # не истекает скоро

            refresh_token = data.get("refresh_token")
            if not refresh_token:
                continue

            try:
                new_tokens = await ory_oauth.refresh_access_token(refresh_token)
                if new_tokens:
                    new_expires_at = datetime.utcnow() + timedelta(
                        seconds=new_tokens.get("expires_in", 3600)
                    )
                    new_access = new_tokens["access_token"]
                    new_refresh = new_tokens.get("refresh_token", refresh_token)

                    # Обновляем in-memory
                    self._tokens[user_id] = {
                        "access_token": new_access,
                        "refresh_token": new_refresh,
                        "expires_at": new_expires_at,
                        "ory_id": data.get("ory_id"),
                    }

                    # Обновляем в БД
                    await save_ory_tokens(
                        chat_id=user_id,
                        access_token=new_access,
                        refresh_token=new_refresh,
                        expires_at=new_expires_at,
                        ory_id=data.get("ory_id"),
                    )
                    refreshed += 1
                else:
                    failed += 1
                    logger.warning(f"Gateway: refresh failed for user {user_id}, removing tokens")
                    del self._tokens[user_id]
            except Exception as e:
                failed += 1
                logger.error(f"Gateway: refresh error for user {user_id}: {e}")

        if refreshed or failed:
            logger.info(f"Gateway: proactive refresh — {refreshed} ok, {failed} failed")

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
        if self._is_circuit_open():
            logger.debug("Gateway: circuit breaker open, skipping")
            return None

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
                    if resp.status == 401:
                        logger.warning(f"Gateway: 401 for user {telegram_user_id} — token expired or invalid")
                        return None
                    if resp.status == 403:
                        logger.warning(f"Gateway: 403 for user {telegram_user_id} — no subscription")
                        return None
                    if resp.status == 200:
                        data = await resp.json()
                        if "result" in data:
                            self._record_success()
                            return data["result"]
                        if "error" in data:
                            error_msg = data["error"].get("message", str(data["error"]))
                            logger.error(f"Gateway JSON-RPC error: {error_msg}")
                            return None
                        logger.warning(f"Gateway: unexpected response: {list(data.keys())}")
                        return None
                    else:
                        error = await resp.text()
                        logger.error(f"Gateway HTTP {resp.status}: {error[:200]}")
                        last_error = f"HTTP {resp.status}"
                        self._record_failure()
            except asyncio.TimeoutError:
                last_error = "timeout"
                if attempt < self.MAX_RETRIES:
                    logger.warning(f"Gateway timeout ({timeout}s), retry {attempt + 1}")
                    await asyncio.sleep(1)
                    continue
                self._record_failure()
            except Exception as e:
                logger.error(f"Gateway exception: {e}")
                self._record_failure()
                return None

        logger.warning("Gateway: unavailable, continuing without MCP")
        return None

    def _parse_text_content(self, result: Optional[dict]) -> Any:
        """Извлекает текстовый контент из MCP result."""
        if not result or "content" not in result:
            return None
        for item in result.get("content", []):
            if item.get("type") == "text":
                try:
                    return json.loads(item.get("text", "null"))
                except json.JSONDecodeError:
                    return item.get("text")
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
        """Поиск по знаниям (L2: Pack, guides, DS)."""
        args: Dict[str, Any] = {"query": query, "limit": limit}
        if source_type:
            args["source_type"] = source_type

        result = await self._call("knowledge_search", args, telegram_user_id)
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


# Singleton
gateway_mcp = GatewayMCPClient(GATEWAY_MCP_URL)
