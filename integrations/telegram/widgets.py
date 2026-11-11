"""Telegram HTML widgets (Bot API 10.4, 2026-11-09).

aiogram 3.29 has no sendWidget yet, so the method is declared here as a raw
TelegramMethod. Widget HTML is static (cannot be edited after sending); all
per-user values travel in ``chat_data`` (<=1024 bytes UTF-8) and are rendered
by the template's own JS.

Spec: https://core.telegram.org/bots/widgets
"""

from __future__ import annotations

import functools
import json
import math
import os
from datetime import date, timedelta
from pathlib import Path

from aiogram.methods.base import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, Message

_TEMPLATES_DIR = Path(__file__).parent / "widget_templates"


class SendWidget(TelegramMethod[Message]):
    """Bot API ``sendWidget`` (only private chats in production for now)."""

    __returning__ = Message
    __api_method__ = "sendWidget"

    chat_id: int
    html: str
    caption: str | None = None
    chat_data: str | None = None
    reply_markup: InlineKeyboardMarkup | None = None


def progress_widget_enabled() -> bool:
    return os.getenv("PROGRESS_WIDGET_ENABLED", "").strip().lower() in {"1", "true", "yes"}


@functools.cache
def load_template(name: str) -> str:
    return (_TEMPLATES_DIR / f"{name}.html").read_text(encoding="utf-8")


def build_progress_chat_data(cache: dict, lang: str) -> str:
    """Pack the /progress overview cache into the widget's chat_data JSON.

    Keys are one letter to stay far below the 1024-byte chat_data limit:
    s streak, r record, w active days this week, t active days total,
    p points (omitted when unknown), c calendar bits oldest->newest,
    o weekday offset of the first calendar day (0 = Monday), k "dd.mm" of the
    Monday that starts each calendar row, l language. The last calendar day is today.
    """
    calendar = cache.get("calendar") or []
    offset, week_labels = 0, []
    if calendar:
        first_day = date.fromisoformat(calendar[0]["date"])
        offset = first_day.weekday()
        first_monday = first_day - timedelta(days=offset)
        rows = math.ceil((offset + len(calendar)) / 7)
        week_labels = [(first_monday + timedelta(weeks=n)).strftime("%d.%m") for n in range(rows)]
    payload = {
        "s": int(cache.get("streak") or 0),
        "r": int(cache.get("longest_streak") or 0),
        "w": int(cache.get("days_active_week") or 0),
        "t": int(cache.get("active_days_total") or 0),
        "c": "".join("1" if day.get("active") else "0" for day in calendar),
        "o": offset,
        "k": week_labels,
        "l": lang if lang in {"ru", "en"} else "en",
    }
    points = cache.get("points_balance")
    if points is not None:
        payload["p"] = int(float(points))

    return json.dumps(payload, separators=(",", ":"))
