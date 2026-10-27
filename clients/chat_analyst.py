from __future__ import annotations

"""
HTTP-клиент сервиса «Аналитик чата» (РП-612). Контракт: chat-analyst-service/CONTRACT.md.

Бот здесь только транспорт: допуск (проверка администратора), границы сбора,
повторы сообщений и срок хранения решает сервис. Тексты сообщений в журналы
не попадают — только коды ответов.
"""

import asyncio

import aiohttp
from config import get_logger

logger = get_logger(__name__)

MESSAGE_TIMEOUT_SECONDS = 3.0      # одна попытка: очереди в боте нет (решение пилота 27.10)
CONTROL_TIMEOUT_SECONDS = 10.0     # register / unregister / status
DIGEST_TIMEOUT_SECONDS = 120.0     # синхронная генерация дайджеста в сервисе
CONTROL_ATTEMPTS = 3
CONTROL_RETRY_DELAY_SECONDS = 2.0


class ChatAnalystError(Exception):
    """Сервис ответил не 2xx или недоступен. `status` = 0 при сетевой ошибке."""

    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"chat-analyst status={status} detail={detail}")
        self.status = status
        self.detail = detail


class ChatAnalystClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, path: str, *, json: dict | None, timeout: float) -> dict:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.request(
                    method, f"{self._base}{path}", json=json, headers=self._headers
                ) as response:
                    body = await _read_json(response)
                    if response.status >= 400:
                        raise ChatAnalystError(response.status, _error_code(body))
                    return body
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ChatAnalystError(0, type(err).__name__) from None

    async def _control(self, method: str, path: str, json: dict | None = None) -> dict:
        """register / unregister: повтор только при сетевой ошибке, 5xx и 429.
        4xx (в том числе 409) возвращается сразу: повтор не изменит ответа."""
        last: ChatAnalystError | None = None
        for attempt in range(CONTROL_ATTEMPTS):
            try:
                return await self._request(method, path, json=json, timeout=CONTROL_TIMEOUT_SECONDS)
            except ChatAnalystError as err:
                if 400 <= err.status < 500 and err.status != 429:
                    raise
                last = err
                if attempt + 1 < CONTROL_ATTEMPTS:
                    await asyncio.sleep(CONTROL_RETRY_DELAY_SECONDS)
        assert last is not None
        raise last

    async def register(self, chat_id: int, added_at: float, notified_at: float) -> None:
        await self._control(
            "POST", f"/chats/{chat_id}/register",
            {"added_at": added_at, "notified_at": notified_at},
        )

    async def unregister(self, chat_id: int) -> None:
        await self._control("POST", f"/chats/{chat_id}/unregister")

    async def status(self, chat_id: int) -> dict:
        return await self._request(
            "GET", f"/chats/{chat_id}/status", json=None, timeout=CONTROL_TIMEOUT_SECONDS
        )

    async def send_message(
        self, chat_id: int, message_id: int, sender_id: int, text: str, sent_at: float
    ) -> None:
        await self._request(
            "POST", f"/chats/{chat_id}/messages",
            json={
                "version": 1, "message_id": message_id, "sender_id": sender_id,
                "text": text, "sent_at": sent_at,
            },
            timeout=MESSAGE_TIMEOUT_SECONDS,
        )

    async def digest(
        self, chat_id: int, requesting_admin_id: int, since: float, participant_id: int | None = None
    ) -> str:
        body: dict = {"requesting_admin_id": requesting_admin_id, "since": since}
        if participant_id is not None:
            body["participant_id"] = participant_id
        reply = await self._request(
            "POST", f"/chats/{chat_id}/digest", json=body, timeout=DIGEST_TIMEOUT_SECONDS
        )
        return str(reply["digest"])


async def _read_json(response: aiohttp.ClientResponse) -> dict:
    try:
        body = await response.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _error_code(body: dict) -> str:
    """Только короткий код ошибки сервиса (`ChatFrozen`); произвольный текст в исключение не берём."""
    detail = body.get("detail")
    return detail if isinstance(detail, str) and detail.isidentifier() and len(detail) <= 64 else ""
