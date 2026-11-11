"""Progress overview as a Telegram HTML widget (WP-5 Ф72)."""

import json
import zlib
from datetime import date, timedelta

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from integrations.telegram.widgets import (
    SendWidget,
    build_progress_chat_data,
    load_template,
    progress_widget_enabled,
)


def _calendar(start: date, pattern: str) -> list[dict]:
    days = [start + timedelta(days=i) for i in range(len(pattern))]
    return [{"date": d.isoformat(), "weekday": d.weekday(), "active": bit == "1"} for d, bit in zip(days, pattern)]


def test_chat_data_packs_overview_fields():
    wednesday = date(2026, 10, 14)
    cache = {
        "name": "Иван",
        "streak": 3,
        "longest_streak": 9,
        "days_active_week": 4,
        "active_days_total": 41,
        "points_balance": "120.0",
        "calendar": _calendar(wednesday, "1101"),
    }

    data = json.loads(build_progress_chat_data(cache, "ru"))

    assert data == {"s": 3, "r": 9, "w": 4, "t": 41, "p": 120, "c": "1101", "o": 2, "l": "ru"}


def test_chat_data_omits_unknown_points_and_falls_back_to_english():
    data = json.loads(build_progress_chat_data({"calendar": []}, "fr"))

    assert "p" not in data
    assert data["l"] == "en"
    assert data["c"] == "" and data["o"] == 0


def test_chat_data_fits_telegram_limit_for_full_calendar():
    cache = {"streak": 999, "longest_streak": 999, "active_days_total": 99999, "points_balance": 10**9,
             "calendar": _calendar(date(2026, 10, 1), "1" * 35)}

    assert len(build_progress_chat_data(cache, "ru").encode()) <= 1024


def test_template_fits_telegram_size_limits():
    html = load_template("progress").encode("utf-8")

    assert len(html) <= 32768
    assert len(zlib.compress(html, 9)) <= 4096
    assert b"\x00" not in html
    assert b"color-mix" not in html  # unsupported by the widget sandbox
    # opacity < 1 marks an element "heavy"; >16 heavy elements stop the widget,
    # and the calendar alone has ~29 cells (seen live 2026-11-11: "Widget stopped").
    assert b"opacity" not in html


def test_send_widget_serializes_as_bot_api_call():
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="progress_back")]])
    method = SendWidget(chat_id=1, html="<p>x</p>", caption="c", chat_data="{}", reply_markup=kb)

    assert method.__api_method__ == "sendWidget"
    dumped = method.model_dump(exclude_none=True)
    assert dumped["chat_id"] == 1 and dumped["html"] == "<p>x</p>" and dumped["chat_data"] == "{}"


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("PROGRESS_WIDGET_ENABLED", raising=False)
    assert progress_widget_enabled() is False
    monkeypatch.setenv("PROGRESS_WIDGET_ENABLED", "1")
    assert progress_widget_enabled() is True


def _progress_state(bot_call):
    from unittest.mock import MagicMock

    from states.utilities.progress import ProgressState

    state = ProgressState.__new__(ProgressState)
    state.bot = MagicMock(side_effect=bot_call)
    return state


def _overview_cache():
    return {"name": "Иван", "streak": 1, "calendar": _calendar(date(2026, 11, 2), "1")}


async def test_overview_widget_sent_when_enabled(monkeypatch):
    monkeypatch.setenv("PROGRESS_WIDGET_ENABLED", "1")
    sent = []

    async def ok(method):
        sent.append(method)

    state = _progress_state(ok)
    kb = InlineKeyboardMarkup(inline_keyboard=[])

    assert await state._send_overview_widget({"chat_id": 42, "language": "ru"}, _overview_cache(), "ru", kb) is True
    assert isinstance(sent[0], SendWidget) and sent[0].chat_id == 42 and "Иван" in sent[0].caption


async def test_overview_widget_falls_back_when_telegram_rejects(monkeypatch):
    monkeypatch.setenv("PROGRESS_WIDGET_ENABLED", "1")

    async def rejected(method):
        raise TelegramBadRequest(method=method, message="Bad Request: method not found")

    state = _progress_state(rejected)

    assert await state._send_overview_widget({"chat_id": 42}, _overview_cache(), "ru", InlineKeyboardMarkup(inline_keyboard=[])) is False


async def test_overview_widget_skipped_when_flag_off(monkeypatch):
    monkeypatch.delenv("PROGRESS_WIDGET_ENABLED", raising=False)

    async def must_not_call(method):
        raise AssertionError("bot must not be called")

    state = _progress_state(must_not_call)

    assert await state._send_overview_widget({"chat_id": 42}, _overview_cache(), "ru", InlineKeyboardMarkup(inline_keyboard=[])) is False
