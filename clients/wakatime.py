"""
WakaTime API клиент — получение статистики рабочего времени.

Использует системный WAKATIME_API_KEY (владелец IWE).
Auth: Basic base64(api_key) — формат WakaTime API v1.

Использование:
    from clients.wakatime import wakatime_client

    day = await wakatime_client.get_day_summary()      # вчера
    week = await wakatime_client.get_week_summary()     # текущая + прошлая неделя
    text = wakatime_client.format_telegram(day, week)   # готовый текст для TG
"""

import base64
from datetime import date, timedelta
from typing import Any

import aiohttp

from config import get_logger

logger = get_logger(__name__)

API_BASE = "https://wakatime.com/api/v1/users/current"


class WakaTimeClient:
    _session: aiohttp.ClientSession | None = None

    def __init__(self):
        self._api_key: str | None = None

    def _get_api_key(self) -> str | None:
        if self._api_key is None:
            from config.settings import WAKATIME_API_KEY
            self._api_key = WAKATIME_API_KEY
        return self._api_key

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    def _headers(self) -> dict | None:
        key = self._get_api_key()
        if not key:
            return None
        encoded = base64.b64encode(key.encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    async def _fetch(self, url: str) -> dict | None:
        headers = self._headers()
        if not headers:
            logger.warning("WAKATIME_API_KEY not configured")
            return None

        session = await self._get_session()
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 401:
                    logger.error("WakaTime API: invalid key (401)")
                    return None
                if resp.status >= 400:
                    logger.error(f"WakaTime API error: {resp.status}")
                    return None
                return await resp.json()
        except aiohttp.ClientError as e:
            logger.error(f"WakaTime request failed: {e}")
            return None

    async def get_day_summary(self, day: date | None = None) -> dict | None:
        """Получить саммари за один день (по умолчанию — вчера)."""
        target = day or (date.today() - timedelta(days=1))
        ds = target.isoformat()
        data = await self._fetch(f"{API_BASE}/summaries?start={ds}&end={ds}")
        if not data:
            return None

        result: dict[str, Any] = {"date": ds}
        result["total"] = data.get("cumulative_total", {}).get("text", "н/д")

        day_data = (data.get("data") or [{}])[0]
        result["projects"] = _aggregate_entries(day_data.get("projects", []))
        result["languages"] = _aggregate_entries(day_data.get("languages", []))[:5]
        return result

    async def get_week_summary(self) -> dict | None:
        """Получить саммари: текущая неделя + предыдущая."""
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        prev_monday = monday - timedelta(days=7)
        prev_sunday = monday - timedelta(days=1)

        resp_this = await self._fetch(
            f"{API_BASE}/summaries?start={monday.isoformat()}&end={today.isoformat()}"
        )
        resp_prev = await self._fetch(
            f"{API_BASE}/summaries?start={prev_monday.isoformat()}&end={prev_sunday.isoformat()}"
        )

        result: dict[str, Any] = {
            "current": {
                "period": f"{monday.isoformat()} — {today.isoformat()}",
                "total": "н/д",
                "projects": [],
            },
            "previous": {
                "period": f"{prev_monday.isoformat()} — {prev_sunday.isoformat()}",
                "total": "н/д",
                "projects": [],
            },
        }

        if resp_this:
            result["current"]["total"] = resp_this.get("cumulative_total", {}).get("text", "н/д")
            result["current"]["projects"] = _aggregate_multi_day(resp_this.get("data", []), "projects")

        if resp_prev:
            result["previous"]["total"] = resp_prev.get("cumulative_total", {}).get("text", "н/д")
            result["previous"]["projects"] = _aggregate_multi_day(resp_prev.get("data", []), "projects")

        return result

    @staticmethod
    def format_telegram(day: dict | None, week: dict | None) -> str:
        """Форматировать результаты для Telegram (Markdown v1)."""
        parts: list[str] = []

        if day:
            lines = [f"*Вчера* ({day['date']}): *{day['total']}*"]
            if day["projects"]:
                lines.append("")
                for p in day["projects"][:7]:
                    lines.append(f"  {p['name']} — {p['text']}")
            parts.append("\n".join(lines))

        if week:
            cur = week["current"]
            prev = week["previous"]
            lines = [f"*Текущая неделя*: *{cur['total']}*"]
            if cur["projects"]:
                lines.append("")
                for p in cur["projects"][:7]:
                    lines.append(f"  {p['name']} — {p['text']}")
            lines.append(f"\n*Прошлая неделя*: *{prev['total']}*")
            parts.append("\n".join(lines))

        if not parts:
            return "WakaTime: нет данных. Проверьте API-ключ."

        return "\U0001f4ca *WakaTime*\n\n" + "\n\n".join(parts)


def _aggregate_entries(entries: list[dict]) -> list[dict]:
    """Сортировка по total_seconds desc, top N."""
    return sorted(entries, key=lambda x: x.get("total_seconds", 0), reverse=True)


def _aggregate_multi_day(days: list[dict], key: str) -> list[dict]:
    """Агрегация записей по нескольким дням."""
    agg: dict[str, int] = {}
    for day in days:
        for entry in day.get(key, []):
            name = entry.get("name", "?")
            agg[name] = agg.get(name, 0) + entry.get("total_seconds", 0)

    result = []
    for name, seconds in sorted(agg.items(), key=lambda x: x[1], reverse=True)[:10]:
        h, remainder = divmod(seconds, 3600)
        m = remainder // 60
        result.append({"name": name, "total_seconds": seconds, "text": f"{h}h {m}m"})
    return result


wakatime_client = WakaTimeClient()
