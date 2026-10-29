from __future__ import annotations

"""
HTTP-клиент службы Image (РП-614): генерация картинок и видео через Higgsfield.

Бот здесь только транспорт: кого пускать, дневной лимит и выбор модели решает
служба (живёт на Цехе за image.aisystant.ai). Тексты запросов и идентификаторы
пользователей в журналы не попадают — только коды ответов.
"""

import asyncio

import aiohttp
from config import get_logger

logger = get_logger(__name__)

REQUEST_TIMEOUT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 5.0
JOB_DEADLINE_SECONDS = 60 * 60.0  # очередь в службе + её собственное ожидание видео до 40 минут
DOWNLOAD_TIMEOUT_SECONDS = 120.0
USER_AGENT = "aist-bot-image/1.0 (WP-614)"


class ImageServiceError(Exception):
    """Служба ответила не 2xx или недоступна. `status` = 0 при сетевой ошибке."""

    def __init__(self, status: int, detail: str = "", limit: int | None = None) -> None:
        super().__init__(f"image-service status={status} detail={detail}")
        self.status = status
        self.detail = detail
        self.limit = limit


class ImageServiceClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, f"{self._base}{path}", json=json, headers=self._headers) as resp:
                    body = await resp.json(content_type=None) if resp.content_length != 0 else {}
                    body = body if isinstance(body, dict) else {}
                    if resp.status >= 400:
                        raise ImageServiceError(resp.status, str(body.get("error", "")), body.get("limit"))
                    return body
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise ImageServiceError(0, type(err).__name__) from None

    async def submit(self, kind: str, prompt: str, user_id: int, username: str | None) -> str:
        body = await self._request("POST", "/v1/jobs", {
            "kind": kind, "prompt": prompt, "user": {"id": str(user_id), "username": username or ""},
        })
        return body["job_id"]

    async def wait(self, job_id: str) -> list[str]:
        """Ждёт завершения задания; возвращает ссылки на готовые файлы."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + JOB_DEADLINE_SECONDS
        while loop.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                job = await self._request("GET", f"/v1/jobs/{job_id}")
            except ImageServiceError as err:
                if err.status == 0:
                    continue  # кратковременный сбой сети не обрывает ожидание
                raise
            if job.get("status") == "done":
                return list(job.get("urls") or [])
            if job.get("status") != "running":
                raise ImageServiceError(200, "job_failed")
        raise ImageServiceError(0, "deadline")


async def download(url: str, max_bytes: int) -> bytes | None:
    """Скачивает готовый файл; None, если он больше max_bytes или недоступен."""
    try:
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": USER_AGENT}) as resp:
                if resp.status != 200 or (resp.content_length or 0) > max_bytes:
                    return None
                # content.read(n) returns only what has arrived so far (29.10: 15 KB of a 9 MB PNG),
                # so the body is collected chunk by chunk until the stream ends.
                data = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        return None
                return bytes(data)
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        logger.warning("[image] download failed: %s", type(err).__name__)
        return None
