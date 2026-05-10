from __future__ import annotations

"""
WakaTime API клиент — получение статистики рабочего времени.

Per-user: каждый пользователь подключает свой WakaTime API key
через Settings → Connections → WakaTime (аналогично GitHub).
Auth: Basic base64(api_key) — формат WakaTime API v1.

Использование:
    from clients.wakatime import wakatime_client

    # Per-user (из БД)
    day = await wakatime_client.get_day_summary(api_key="waka_xxx")

    # Валидация ключа при подключении
    user = await wakatime_client.validate_key("waka_xxx")
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

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    @staticmethod
    def _make_headers(api_key: str) -> dict:
        # OAuth token (waka_tok_xxx) → Bearer Auth. API key (waka_xxx) → Basic Auth.
        if api_key.startswith("waka_tok_"):
            return {"Authorization": f"Bearer {api_key}"}
        else:
            encoded = base64.b64encode(api_key.encode()).decode()
            return {"Authorization": f"Basic {encoded}"}

    async def _fetch(self, url: str, api_key: str) -> dict | None:
        session = await self._get_session()
        try:
            async with session.get(url, headers=self._make_headers(api_key)) as resp:
                if resp.status == 401:
                    logger.warning("WakaTime API: invalid key (401)")
                    return None
                if resp.status >= 400:
                    logger.error(f"WakaTime API error: {resp.status}")
                    return None
                return await resp.json()
        except aiohttp.ClientError as e:
            logger.error(f"WakaTime request failed: {e}")
            return None

    async def validate_key(self, api_key: str) -> dict | None:
        """Проверить ключ, вернуть username если валидный."""
        data = await self._fetch(f"{API_BASE}", api_key)
        if data and "data" in data:
            user_data = data["data"]
            return {
                "username": user_data.get("username"),
                "display_name": user_data.get("display_name"),
            }
        return None

    async def get_day_summary(self, api_key: str, day: date | None = None, today: bool = False) -> dict | None:
        """Получить саммари за один день (по умолчанию — вчера, today=True — сегодня)."""
        if today:
            target = date.today()
        else:
            target = day or (date.today() - timedelta(days=1))
        ds = target.isoformat()
        data = await self._fetch(f"{API_BASE}/summaries?start={ds}&end={ds}", api_key)
        if not data:
            return None

        result: dict[str, Any] = {"date": ds}
        result["total"] = data.get("cumulative_total", {}).get("text", "н/д")

        day_data = (data.get("data") or [{}])[0]
        result["projects"] = _aggregate_entries(day_data.get("projects", []))
        result["languages"] = _aggregate_entries(day_data.get("languages", []))[:5]
        return result

    async def get_week_summary(self, api_key: str) -> dict | None:
        """Получить саммари: текущая неделя + предыдущая."""
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        prev_monday = monday - timedelta(days=7)
        prev_sunday = monday - timedelta(days=1)

        resp_this = await self._fetch(
            f"{API_BASE}/summaries?start={monday.isoformat()}&end={today.isoformat()}", api_key
        )
        resp_prev = await self._fetch(
            f"{API_BASE}/summaries?start={prev_monday.isoformat()}&end={prev_sunday.isoformat()}", api_key
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
    def _esc(text: str) -> str:
        """Экранирование спецсимволов Markdown v1 в динамическом тексте."""
        for ch in ('_', '*', '`', '['):
            text = text.replace(ch, f'\\{ch}')
        return text

    @staticmethod
    def format_telegram(today: dict | None, yesterday: dict | None, week: dict | None) -> str:
        """Форматировать результаты для Telegram (Markdown v1)."""
        esc = WakaTimeClient._esc
        parts: list[str] = []

        if today:
            lines = [f"*Сегодня* ({today['date']}): *{esc(today['total'])}*"]
            if today["projects"]:
                lines.append("")
                for p in today["projects"][:5]:
                    lines.append(f"  {esc(p['name'])} — {esc(p['text'])}")
            parts.append("\n".join(lines))

        if yesterday:
            lines = [f"*Вчера* ({yesterday['date']}): *{esc(yesterday['total'])}*"]
            if yesterday["projects"]:
                lines.append("")
                for p in yesterday["projects"][:5]:
                    lines.append(f"  {esc(p['name'])} — {esc(p['text'])}")
            parts.append("\n".join(lines))

        if week:
            cur = week["current"]
            prev = week["previous"]
            lines = [f"*Текущая неделя*: *{esc(cur['total'])}*"]
            if cur["projects"]:
                lines.append("")
                for p in cur["projects"][:7]:
                    lines.append(f"  {esc(p['name'])} — {esc(p['text'])}")
            lines.append(f"\n*Прошлая неделя*: *{esc(prev['total'])}*")
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
        result.append({"name": name, "total_seconds": seconds, "text": f"{int(h)}h {int(m)}m"})
    return result


wakatime_client = WakaTimeClient()
