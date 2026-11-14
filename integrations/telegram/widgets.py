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
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram.methods.base import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, Message

from core.activity_board import last_7_days
from core.qualification import qualified_level
from db.queries.cp_assessment import STAGE_NAMES

_TEMPLATES_DIR = Path(__file__).parent / "widget_templates"
_MOSCOW = ZoneInfo("Europe/Moscow")


class SendWidget(TelegramMethod[Message]):
    """Bot API ``sendWidget`` (only private chats in production for now)."""

    __returning__ = Message
    __api_method__ = "sendWidget"

    chat_id: int
    html: str
    caption: str | None = None
    chat_data: str | None = None
    reply_markup: InlineKeyboardMarkup | None = None


class WidgetRefusalBreaker:
    """Stops calling sendWidget while Telegram refuses widgets to the bot as a whole.

    After refusals (INPUT_MEDIA_WIDGET_NOT_ALLOWED, seen for every chat from 2026-11-13 16:50Z)
    from THRESHOLD different chats with no success in between, the bot skips the call for PAUSE_S
    seconds; one chat refused again and again does not switch widgets off for everyone. The first
    call after the pause is the only probe (the pause is re-armed when it goes out): a refusal
    keeps the pause, a success clears everything. Other send errors do not count. State lives in
    process memory; no await inside, so concurrent /progress calls cannot interleave here.
    """

    THRESHOLD = 3
    PAUSE_S = 300.0
    REFUSAL = "INPUT_MEDIA_WIDGET_NOT_ALLOWED"

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._refusing_chats: set[int] = set()
        self._paused_until = 0.0

    def _tripped(self) -> bool:
        return len(self._refusing_chats) >= self.THRESHOLD

    def allows(self) -> bool:
        now = self._clock()
        if now < self._paused_until:
            return False
        if self._tripped():
            self._paused_until = now + self.PAUSE_S  # this call is the probe; others wait
        return True

    def record_success(self) -> None:
        self._refusing_chats.clear()
        self._paused_until = 0.0

    def record_failure(self, error: Exception, chat_id: int) -> bool:
        """Count a refusal; True when this failure starts a pause."""
        if self.REFUSAL not in str(error):
            return False
        was_tripped = self._tripped()
        self._refusing_chats.add(chat_id)
        if not self._tripped():
            return False
        self._paused_until = self._clock() + self.PAUSE_S
        return not was_tripped


widget_breaker = WidgetRefusalBreaker()


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
    Monday that starts each calendar row, l language, m places per day (hex digit per
    calendar day, bits iwe 1 / club 2 / lms 4 / bot 8; omitted without ecosystem data),
    h "dd.mm–dd.mm" of the last 7 calendar days and q their place masks (hex digit per day,
    bot-only days already carry the bot bit; core/activity_board.last_7_days) for the 7-days tab,
    f qualification name for «Работник» and above (then no stage keys),
    e total points earned (never decreases on redemption), g stage 1..5 of the latest
    valid diagnostic, n its name, d "dd.mm" (Moscow) of that diagnostic; e/g/n/d are
    omitted when unknown (growth tab, WP-417 Ф8).
    The last calendar day is today.
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
    week = last_7_days(calendar)
    if week:
        payload["h"] = week["range"]
        payload["q"] = "".join(format(mask, "x") for mask in week["masks"])
    if any("mask" in day for day in calendar):
        # one hex digit per day: which places the day came from (core/activity_board.py bits)
        payload["m"] = "".join(format(int(day.get("mask") or 0), "x") for day in calendar)
    points = cache.get("points_balance")
    if points is not None:
        payload["p"] = int(float(points))
    earned = cache.get("earned_total")
    if earned is not None:
        payload["e"] = int(float(earned))
    payload.update(growth_fields(cache, payload["l"]))

    return json.dumps(payload, separators=(",", ":"))


def growth_fields(cache: dict, lang: str) -> dict:
    """Growth keys: {"f": qualification} for «Работник» and above, otherwise stage keys g/n/d."""
    qualification = qualified_level(cache.get("degree"))
    if qualification:
        return {"f": qualification}
    return stage_fields(cache.get("cp_profile"), lang)


def stage_fields(cp_profile: dict | None, lang: str) -> dict:
    """Stage keys g/n/d from the cached diagnostic profile; empty when there is none.

    Shared by the widget and the /progress text card; a language without stage names falls back
    to Russian, like i18n.t() does for the rest of the card.

    The profile is loaded with only_valid=True, so an expired diagnostic arrives as None.
    An unreadable assessment date drops only the date: this path must not break /progress.
    """
    if not isinstance(cp_profile, dict):
        return {}
    names = STAGE_NAMES.get(lang, STAGE_NAMES["ru"])
    stage = cp_profile.get("stage")
    if type(stage) is not int or stage not in names:
        return {}
    fields = {"g": stage, "n": names[stage]}
    try:
        assessed = datetime.fromisoformat(str(cp_profile["assessed_at"]))
    except (KeyError, ValueError):
        return fields
    if assessed.tzinfo is None:  # TIMESTAMPTZ always has a zone; a bare value is UTC
        assessed = assessed.replace(tzinfo=timezone.utc)
    fields["d"] = assessed.astimezone(_MOSCOW).strftime("%d.%m")
    return fields
