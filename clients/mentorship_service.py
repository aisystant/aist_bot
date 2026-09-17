from __future__ import annotations

"""
Клиент standalone-сервиса рабочего места наставника (WP-578 Ф3).

Отдельный сервис на Railway (не Cloudflare Worker — редизайн 17.09, см.
DS-my-strategy/inbox/WP-578/WP-578.md), тот же паттерн, что clients/checklist_mcp.py:
статический сервисный токен + доверенный заголовок X-Mentor-Account-Id (личность
вызывающего наставника уже проверена ботом через resolve_ory_id_from_chat до
вызова сюда — сервис не делает Ory-аутентификацию сам).

Использование:
    from clients.mentorship_service import mentorship_service

    card = await mentorship_service.get_participant_card(mentor_account_id, stream_id, participant_account_id)
    result = await mentorship_service.add_participant_note(
        mentor_account_id, stream_id, participant_account_id, body, quarantine_confirmed=True,
    )
"""

import logging

import aiohttp

from config.settings import (
    MENTORSHIP_SERVICE_TIMEOUT,
    MENTORSHIP_SERVICE_TOKEN,
    MENTORSHIP_SERVICE_URL,
)

logger = logging.getLogger(__name__)


class MentorshipServiceError(Exception):
    """Сервис ответил ошибкой уровня приложения (invalid_params/access_denied/…).

    kind — категория из ответа сервиса (KIND_TO_HTTP_STATUS в mcp-handler.ts),
    для хендлера, который решает какой текст показать наставнику.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class MentorshipServiceClient:
    """HTTP-клиент mentorship-service. Два тула: get_participant_card, add_participant_note."""

    def __init__(self, url: str, service_token: str, timeout: int):
        self.url = url.rstrip("/")
        self._service_token = service_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def is_configured(self) -> bool:
        """False, если MENTORSHIP_SERVICE_URL/токен ещё не выставлены в env."""
        return bool(self.url and self._service_token)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _call_tool(self, mentor_account_id: str, name: str, arguments: dict) -> dict | None:
        """Общий вызов POST /mcp/tools/call. None — сеть/сервис недоступен
        (причина в логе). MentorshipServiceError — сервис ответил ошибкой
        уровня приложения (invalid_params/access_denied/…) — вызывающий
        хендлер решает, что сказать наставнику."""
        if not self.is_configured():
            logger.warning("[mentorship-service] запрос %s пропущен: сервис не настроен (нет URL/токена)", name)
            return None

        try:
            session = await self._get_session()
            async with session.post(
                f"{self.url}/mcp/tools/call",
                headers={
                    "Authorization": f"Bearer {self._service_token}",
                    "X-Mentor-Account-Id": mentor_account_id,
                },
                json={"name": name, "arguments": arguments},
            ) as resp:
                body = await resp.json()
                if resp.status != 200:
                    error_message = body.get("error", "unknown error")
                    logger.warning("[mentorship-service] запрос %s: HTTP %s — %s", name, resp.status, error_message)
                    raise MentorshipServiceError(_HTTP_STATUS_TO_KIND.get(resp.status, "internal"), error_message)
                return body.get("content")
        except aiohttp.ClientError as e:
            logger.warning("[mentorship-service] запрос %s: сеть — %s", name, e)
            return None

    async def get_participant_card(self, mentor_account_id: str, stream_id: str, participant_account_id: str) -> dict | None:
        return await self._call_tool(
            mentor_account_id,
            "get_participant_card",
            {"streamId": stream_id, "participantAccountId": participant_account_id},
        )

    async def add_participant_note(
        self,
        mentor_account_id: str,
        stream_id: str,
        participant_account_id: str,
        body: str,
        *,
        source_hint: str | None = None,
        quarantine_confirmed: bool,
    ) -> dict | None:
        arguments = {
            "streamId": stream_id,
            "participantAccountId": participant_account_id,
            "body": body,
            "quarantineConfirmed": quarantine_confirmed,
        }
        if source_hint is not None:
            arguments["sourceHint"] = source_hint
        return await self._call_tool(mentor_account_id, "add_participant_note", arguments)


_HTTP_STATUS_TO_KIND = {400: "invalid_params", 403: "access_denied"}

mentorship_service = MentorshipServiceClient(
    url=MENTORSHIP_SERVICE_URL,
    service_token=MENTORSHIP_SERVICE_TOKEN,
    timeout=MENTORSHIP_SERVICE_TIMEOUT,
)
