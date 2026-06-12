"""
HTTP-адаптер к TailorEngine Railway FastAPI service (WP-262 В1.1).

Вызывает POST /tailor/assemble-lesson, возвращает LessonPacket.
Auth: X-Shared-Secret header (WP-402 паттерн).

Используется через EngineRouter при TAILOR_ROUTE=platform.
"""

import logging

import httpx

from .schemas import LessonPacket, LessonRequest

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 10.0
_MAX_RETRIES = 1


class PlatformTailorAdapter:
    """HTTP-клиент к Railway tailor-service.

    Отвечает только за HTTP round-trip и типизацию ответа.
    Telegram-доставка остаётся в BotTailorAdapter.deliver().
    """

    def __init__(self, url: str, shared_secret: str) -> None:
        self._url = url.rstrip("/")
        self._secret = shared_secret

    async def assemble_lesson(self, request: LessonRequest) -> LessonPacket:
        """Собрать занятие через удалённый сервис.

        Raises:
            ConnectionError: сервис недоступен → EngineRouter делает fallback на local
            httpx.HTTPStatusError: сервис вернул 4xx/5xx
        """
        headers = {
            "X-Shared-Secret": self._secret,
            "Content-Type": "application/json",
        }
        last_exc: Exception = ConnectionError("not attempted")

        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
                    resp = await client.post(
                        f"{self._url}/tailor/assemble-lesson",
                        content=request.model_dump_json(),
                        headers=headers,
                    )
                    resp.raise_for_status()
                    return LessonPacket.model_validate(resp.json())
            except httpx.ConnectError as exc:
                last_exc = ConnectionError(str(exc))
                logger.warning(
                    "[Tailor/Platform] connect failed attempt=%d url=%s: %s",
                    attempt + 1,
                    self._url,
                    exc,
                )
            except httpx.HTTPStatusError:
                raise

        raise last_exc
