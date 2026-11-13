"""Progress overview as a Telegram HTML widget (WP-5 Ф72)."""

import json
import re
import shutil
import subprocess
import zlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db.queries.cp_assessment import STAGE_NAMES
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

    assert data == {"s": 3, "r": 9, "w": 4, "t": 41, "p": 120, "c": "1101", "o": 2, "k": ["12.10"], "l": "ru",
                    "h": "14.10–17.10"}


def test_chat_data_labels_each_calendar_row_with_its_monday():
    # 29 days from Wednesday 14.10 to Wednesday 11.11: 5 rows, Mondays 12.10 .. 09.11
    data = json.loads(build_progress_chat_data({"calendar": _calendar(date(2026, 10, 14), "0" * 29)}, "ru"))

    assert data["k"] == ["12.10", "19.10", "26.10", "02.11", "09.11"]


def test_chat_data_omits_unknown_points_and_falls_back_to_english():
    data = json.loads(build_progress_chat_data({"calendar": []}, "fr"))

    assert "p" not in data
    assert data["l"] == "en"
    assert data["c"] == "" and data["o"] == 0 and data["k"] == []


def test_chat_data_fits_telegram_limit_for_full_calendar():
    calendar = [dict(day, mask=15) for day in _calendar(date(2026, 10, 1), "1" * 35)]
    cache = {"streak": 999, "longest_streak": 999, "active_days_total": 99999, "points_balance": 10**12,
             "earned_total": 10**12, "calendar": calendar,
             "cp_profile": {"stage": 4, "assessed_at": "2026-11-01T21:30:00+00:00"}}

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


def test_chat_data_growth_fields_from_profile_and_points():
    cache = {"calendar": _calendar(date(2026, 11, 7), "1" * 7), "earned_total": "250.0",
             "cp_profile": {"stage": 3, "assessed_at": "2026-11-01T21:30:00+00:00"}}

    data = json.loads(build_progress_chat_data(cache, "en"))

    # string from cache, Decimal from DB: both become int; 21:30Z is already 02.11 in Moscow
    assert (data["e"], data["g"], data["n"], data["d"]) == (250, 3, "Systematic", "02.11")
    assert data["h"] == "07.11–13.11"
    assert json.loads(build_progress_chat_data(dict(cache, earned_total=Decimal("7.5")), "ru"))["e"] == 7


@pytest.mark.parametrize("profile", [None, {}, {"stage": None}, {"stage": 0}, {"stage": 6}, {"stage": True}, {"stage": "3"}])
def test_chat_data_omits_stage_without_valid_profile(profile):
    data = json.loads(build_progress_chat_data({"calendar": [], "cp_profile": profile}, "ru"))

    assert not {"g", "n", "d", "e", "h"} & data.keys()


@pytest.mark.parametrize("profile, expected", [
    ({"stage": 2, "assessed_at": "not a date"}, {"g": 2, "n": "Практикующий"}),
    ({"stage": 2, "assessed_at": datetime(2026, 11, 1, 21, 30, tzinfo=timezone.utc)}, {"g": 2, "n": "Практикующий", "d": "02.11"}),
    ({"stage": 2, "assessed_at": "2026-11-01T21:30:00"}, {"g": 2, "n": "Практикующий", "d": "02.11"}),  # naive = UTC
    ({"stage": 2}, {"g": 2, "n": "Практикующий"}),
    (["stage", 2], {}),
])
def test_chat_data_stage_survives_unexpected_profile_shapes(profile, expected):
    data = json.loads(build_progress_chat_data({"calendar": [], "cp_profile": profile}, "ru"))

    assert {k: data[k] for k in ("g", "n", "d") if k in data} == expected


def test_template_has_no_heavy_styles():
    # opacity, filters and blurred shadows make an element "heavy"; more than 16 stop the widget
    html = load_template("progress")

    assert not re.search(r"opacity|filter|blur|box-shadow:(?!inset 0 0 0 )", html)


def test_stage_names_shared_with_diagnose_and_profile_screen():
    from handlers.diagnose import STAGE_NAMES as diagnose_names
    from states.utilities.progress import ProgressState

    assert diagnose_names is STAGE_NAMES["ru"] is ProgressState._CP_STAGE_LABELS
    assert STAGE_NAMES["ru"].keys() == STAGE_NAMES["en"].keys() == {1, 2, 3, 4, 5}


async def test_fetch_points_reads_balance_and_earned_total_in_one_query(monkeypatch):
    from db.queries import rewards
    from states.utilities.progress import ProgressState

    calls = []

    async def totals(account_id):
        calls.append(account_id)
        return {"points": Decimal(40), "earned_total": Decimal(90)}

    monkeypatch.setattr(rewards, "get_point_totals", totals)
    state = ProgressState.__new__(ProgressState)

    assert await state._fetch_points("acc") == {"points": Decimal(40), "earned_total": Decimal(90)}
    assert await state._fetch_points(None) == {"points": None, "earned_total": None}
    assert calls == ["acc"]


async def test_fetch_points_degrades_to_empty_on_error(monkeypatch):
    from db.queries import rewards
    from states.utilities.progress import ProgressState

    async def broken(account_id):
        raise ConnectionError("rewards down")

    monkeypatch.setattr(rewards, "get_point_totals", broken)

    assert await ProgressState.__new__(ProgressState)._fetch_points("acc") == {"points": None, "earned_total": None}


# Template JS runs in node with a fake DOM: acceptance check, the bot CI has no node.
_NODE_HARNESS = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const script = html.split("<script>")[1].split("</script>")[0];
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const out = [];
for (const c of cases) {
  const root = {innerHTML: "", addEventListener: (n, f) => { root[n] = f; }};
  const handlers = {};
  global.document = {getElementById: () => root};
  global.TelegramWidget = {chatData: c, onEvent: (n, f) => { handlers[n] = f; }};
  (0, eval)(script);
  const views = [root.innerHTML];
  const press = (t) => root.click({target: {closest: (sel) => sel === "button[data-t]" ? {getAttribute: () => String(t)} : null}});
  for (const t of [1, 2]) { press(t); views.push(root.innerHTML); }
  handlers.chatDataChanged();
  views.push(root.innerHTML);
  out.push(views);
}
process.stdout.write(JSON.stringify(out));
"""


def _render_tabs(cases: list[str]) -> list[list[str]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: template JS is checked at acceptance, not in CI")
    template = Path(__file__).resolve().parents[1] / "integrations/telegram/widget_templates/progress.html"
    result = subprocess.run([node, "-e", _NODE_HARNESS, str(template)], input=json.dumps(cases),
                            capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def _cells(view: str, place: str) -> list[bool]:
    """On/off cells of one row of the 7-days tab, read from the rendered HTML."""
    row = view.split(f'<span class="k">{place}</span>')[1].split("<span>")[0]
    return ["style=" in cell for cell in row.split("<i")[1:]]


def test_template_tabs_render_for_every_data_shape():
    full = {"s": 5, "r": 9, "w": 3, "t": 40, "c": "0" * 22 + "1101111", "o": 2, "k": [], "l": "ru",
            "h": "07.11–13.11", "m": "0" * 22 + "3100208", "g": 4, "n": "Дисциплинированный", "d": "02.11", "e": 250}
    cases = [
        full,
        {k: v for k, v in full.items() if k != "m"},                  # gateway unavailable: bot row only
        dict(full, m="0" * 29),                                       # gateway answered, nothing elsewhere
        {"s": 0, "c": "", "o": 0, "k": [], "l": "en"},                # empty calendar, pre-Ф8 keys only
        dict(full, c="1", o=0, m="0", h="09.11–09.11"),               # Monday, single bot-only day
        {k: v for k, v in full.items() if k not in ("g", "n", "d")},  # no valid diagnostic
        {k: v for k, v in full.items() if k != "e"},                  # no points row
        dict(full, l="en", n="Disciplined"),
    ]
    views = _render_tabs([json.dumps(c) for c in cases])

    calendar, week, growth, redrawn = views[0]
    assert "Активные действия по неделям" in calendar and "Последние 7 дней по местам · 07.11–13.11" in week
    assert redrawn == growth  # chatDataChanged keeps the selected tab
    # last 7 masks "3100208": IWE days 0, 1; club days 0, 4; bot day 6; days 3 and 5 are bot-only (c=1, mask=0)
    assert _cells(week, "IWE") == [True, True, False, False, False, False, False]
    assert _cells(week, "клуб") == [True, False, False, False, True, False, False]
    assert _cells(week, "бот") == [False, False, False, True, False, True, True]
    assert "Ступень 4 — Дисциплинированный" in growth and "оценка от 02.11" in growth and "250" in growth

    no_gateway = views[1][1]
    assert "Другие места недоступны" in no_gateway and '<span class="k">IWE</span>' not in no_gateway
    assert _cells(no_gateway, "бот") == [True, True, False, True, True, True, True]
    assert "В других местах активности нет" in views[2][1]
    assert "No data yet" in views[3][1] and "No valid diagnostic yet" in views[3][2]
    assert _cells(views[4][1], "бот") == [True] and "<span>Пн</span>" in views[4][1]
    assert "Диагностика не пройдена или устарела — /diagnose" in views[5][2]
    assert "всего баллов" not in views[6][2]
    assert "Stage 4 — Disciplined" in views[7][2] and "Growth" in views[7][0]
