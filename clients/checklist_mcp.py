from __future__ import annotations

"""
Клиент standalone-сервиса read-model чек-листа участника (WP-522 §3в).

Отдельный сервис (не gateway-mcp — тот передан другой команде 12.08.2026,
блокер на новые инструменты). Авторизация — статический сервисный токен
(не Ory JWT пользователя): бот доверенный потребитель, может запросить
чек-лист любого участника по account_id.

Использование:
    from clients.checklist_mcp import checklist_mcp

    status = await checklist_mcp.get_checklist_status(account_id)
    if status is not None:
        ...
"""

import logging

import aiohttp

from config.settings import (
    CHECKLIST_MCP_SERVICE_TOKEN_ONBOARDER,
    CHECKLIST_MCP_TIMEOUT,
    CHECKLIST_MCP_URL,
)

logger = logging.getLogger(__name__)


class ChecklistMCPClient:
    """HTTP-клиент checklist-mcp. Один тул: get_checklist_status."""

    def __init__(self, url: str, service_token: str, timeout: int):
        self.url = url.rstrip("/")
        self._service_token = service_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def is_configured(self) -> bool:
        """False, если ORY_URL/токен ещё не выставлены в env (например, локальная разработка)."""
        return bool(self.url and self._service_token)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_checklist_status(self, account_id: str) -> dict | None:
        """Чек-лист участника (WP-522 §3в: facts + summary) или None при отказе/ошибке.

        None — единственный сигнал отказа для вызывающего (сервис недоступен,
        не настроен, вернул ошибку). Причина всегда в логе.
        """
        if not self.is_configured():
            logger.warning("[checklist-mcp] запрос %s пропущен: сервис не настроен (нет URL/токена)", account_id)
            return None

        try:
            session = await self._get_session()
            async with session.post(
                f"{self.url}/mcp/tools/call",
                headers={"Authorization": f"Bearer {self._service_token}"},
                json={"name": "get_checklist_status", "arguments": {"target_account_id": account_id}},
            ) as resp:
                body = await resp.json()
                if resp.status != 200:
                    logger.warning(
                        "[checklist-mcp] запрос %s: HTTP %s — %s", account_id, resp.status, body.get("error")
                    )
                    return None
                return body.get("content")
        except aiohttp.ClientError as e:
            logger.warning("[checklist-mcp] запрос %s: сеть — %s", account_id, e)
            return None
        except Exception:
            logger.exception("[checklist-mcp] запрос %s: неожиданная ошибка", account_id)
            return None


checklist_mcp = ChecklistMCPClient(
    url=CHECKLIST_MCP_URL,
    service_token=CHECKLIST_MCP_SERVICE_TOKEN_ONBOARDER,
    timeout=CHECKLIST_MCP_TIMEOUT,
)
