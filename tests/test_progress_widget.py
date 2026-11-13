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

from core.activity_board import last_7_days
from db.queries.cp_assessment import STAGE_NAMES
from integrations.telegram.widgets import (
    SendWidget,
    WidgetRefusalBreaker,
    build_progress_chat_data,
    load_template,
    progress_widget_enabled,
)
from states.utilities.progress import overview_growth_line, overview_week_line


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
                    "h": "14.10–17.10", "q": "8808"}


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
    assert all(names.keys() == {1, 2, 3, 4, 5} for names in STAGE_NAMES.values())


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


async def test_get_point_totals_reads_both_fields_from_one_row(monkeypatch):
    from db.queries import rewards

    queries = []

    class Conn:
        async def fetchrow(self, sql, account_id):
            queries.append((sql, account_id))
            return {"points": Decimal("40"), "earned_total": Decimal("90")} if account_id == "acc" else None

    class Pool:
        def acquire(self):
            conn = Conn()

            class Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *exc):
                    return False

            return Ctx()

    async def pool():
        return Pool()

    monkeypatch.setattr(rewards, "get_rewards_pool", pool)

    assert await rewards.get_point_totals("acc") == {"points": Decimal("40"), "earned_total": Decimal("90")}
    assert await rewards.get_point_totals("nobody") is None
    assert await rewards.get_point_totals(None) is None
    assert len(queries) == 2 and "points, earned_total" in queries[0][0]


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
  const root = {innerHTML: ""};
  const handlers = {};
  global.document = {getElementById: () => root};
  global.TelegramWidget = {chatData: c, onEvent: (n, f) => { handlers[n] = f; }};
  (0, eval)(script);
  const views = [root.innerHTML];
  // press = run the inline onclick of the rendered button, as the widget sandbox does
  const press = (t) => { const m = root.innerHTML.match(new RegExp('<button onclick="(tab\\(' + t + '\\))"')); if (!m) throw new Error("no tab button " + t); (0, eval)(m[1]); };
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


def _count(view: str, place: str) -> int:
    """Day count printed at the end of one row of the 7-days tab."""
    row = view.split(f'<span class="k">{place}</span>')[1]
    return int(row.split("</i><span>")[1].split("</span>")[0])


# 29 days from Wednesday 14.10 to Wednesday 11.11; the last 7 run Thursday 05.11 .. Wednesday 11.11
_START = date(2026, 10, 14)
# last 7 masks: IWE+club, IWE, LMS, none, club, none, bot; days 3 and 5 are active by the bot's own journal
_MASKS = [0] * 22 + [3, 1, 4, 0, 2, 0, 8]


def _board(active: str = "0" * 22 + "1111111", masks: list[int] | None = _MASKS, start: date = _START, **extra) -> dict:
    calendar = _calendar(start, active)
    if masks is not None:
        for day, mask in zip(calendar, masks):
            day["mask"] = mask
    return {"streak": 5, "longest_streak": 9, "active_days_total": 40, "days_active_week": 3,
            "calendar": calendar, "earned_total": Decimal("250"),
            "cp_profile": {"stage": 4, "assessed_at": "2026-11-01T21:30:00+00:00"}, **extra}


def test_last_7_days_counts_places_and_bot_only_days():
    week = last_7_days(_board()["calendar"])

    assert week["range"] == "05.11–11.11"
    assert week["masks"] == [3, 1, 4, 8, 2, 8, 8]  # active days without a place become bot days
    assert week["counts"] == {"iwe": 2, "club": 2, "lms": 1, "bot": 3}
    assert week["has_places"] is True
    assert last_7_days(_board(masks=None)["calendar"])["has_places"] is False
    assert last_7_days([]) is None


def test_overview_text_lines_ru_en_and_fallbacks():
    board = _board()

    assert overview_week_line(board["calendar"], "ru") == "📍 Последние 7 дней, 05.11–11.11: IWE 2 · клуб 2 · LMS 1 · бот 3"
    assert overview_week_line(_board(masks=None)["calendar"], "ru") == (
        "📍 Последние 7 дней, 05.11–11.11: бот 7 · IWE, клуб, LMS недоступны")
    assert overview_week_line([], "ru") is None
    assert overview_growth_line(board, "ru") == "📈 Ступень 4 — Дисциплинированный (оценка от 02.11) · Всего баллов: 250"
    assert overview_growth_line(board, "en") == "📈 Stage 4 — Disciplined (assessed 02.11) · Total points: 250"
    assert overview_growth_line(dict(board, cp_profile=None, earned_total=None), "ru") == (
        "📈 Ступень: диагностика не пройдена или устарела — /diagnose")
    assert "Etapa 4 — Disciplinado" in overview_growth_line(board, "es")
    assert "Ступень 4 — Дисциплинированный" in overview_growth_line(board, "de")  # unknown language: Russian, as i18n
    assert overview_growth_line(dict(board, earned_total="NaN"), "ru").endswith("(оценка от 02.11)")


async def test_text_card_shows_week_and_growth_when_widget_is_not_sent(monkeypatch):
    from states.utilities.progress import ProgressState

    state = ProgressState.__new__(ProgressState)
    shown = []

    async def no_widget(*args):
        return False

    async def show(user, text, keyboard, callback=None):
        shown.append(text)

    monkeypatch.setattr(state, "_send_overview_widget", no_widget)
    monkeypatch.setattr(state, "_show_section", show)

    await state._render_overview({"chat_id": 1}, dict(_board(), name="Иван", points_balance=120), "ru")

    assert "🏆 Бонусы: 120\n📍 Последние 7 дней, 05.11–11.11: IWE 2 · клуб 2 · LMS 1 · бот 3\n📈 Ступень 4" in shown[0]


def test_widget_breaker_pauses_after_three_chats_refused_and_sends_one_probe():
    now = [0.0]
    breaker = WidgetRefusalBreaker(clock=lambda: now[0])
    refusal = TelegramBadRequest(method=SendWidget(chat_id=1, html="x"), message="Bad Request: INPUT_MEDIA_WIDGET_NOT_ALLOWED")

    assert breaker.record_failure(ValueError("chat_data too long"), 1) is False  # other errors do not count
    assert [breaker.record_failure(refusal, 7) for _ in range(5)] == [False] * 5  # one chat alone never trips
    assert breaker.allows() is True
    assert [breaker.record_failure(refusal, chat) for chat in (8, 9)] == [False, True]
    assert breaker.allows() is False
    now[0] = WidgetRefusalBreaker.PAUSE_S
    assert [breaker.allows(), breaker.allows()] == [True, False]  # exactly one probe after the pause
    assert breaker.record_failure(refusal, 7) is False and breaker.allows() is False  # probe refused: still paused
    now[0] *= 3
    assert breaker.allows() is True
    breaker.record_success()
    assert breaker.allows() is True and breaker.record_failure(refusal, 7) is False  # count starts over


async def test_overview_skips_telegram_while_breaker_is_paused(monkeypatch):
    from integrations.telegram import widgets

    monkeypatch.setenv("PROGRESS_WIDGET_ENABLED", "1")
    now = [0.0]
    monkeypatch.setattr(widgets, "widget_breaker", WidgetRefusalBreaker(clock=lambda: now[0]))
    calls = []

    async def refused(method):
        calls.append(method)
        raise TelegramBadRequest(method=method, message="Bad Request: INPUT_MEDIA_WIDGET_NOT_ALLOWED")

    state = _progress_state(refused)
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for chat in (41, 42, 43, 44):
        assert await state._send_overview_widget({"chat_id": chat}, _overview_cache(), "ru", kb) is False

    assert len(calls) == 3  # the fourth /progress went straight to the text card


def test_template_tabs_render_for_every_data_shape():
    cases = [
        (_board(), "ru"),
        (_board(masks=None), "ru"),                                   # gateway unavailable: bot row only
        (_board(masks=[0] * 29), "ru"),                               # gateway answered, nothing elsewhere
        ({"calendar": []}, "en"),                                     # empty calendar
        (_board(active="1", masks=[0], start=date(2026, 11, 9)), "ru"),  # Monday, single bot-only day
        (_board(cp_profile=None), "ru"),                              # no valid diagnostic
        (_board(earned_total=None), "ru"),                            # no points row
        (_board(), "en"),
    ]
    views = _render_tabs([build_progress_chat_data(cache, lang) for cache, lang in cases])

    calendar, week, growth, redrawn = views[0]
    assert "Активные действия по неделям" in calendar and "Последние 7 дней по местам · 05.11–11.11" in week
    assert redrawn == growth  # chatDataChanged keeps the selected tab
    assert _cells(week, "IWE") == [True, True, False, False, False, False, False]
    assert _cells(week, "клуб") == [True, False, False, False, True, False, False]
    assert _cells(week, "LMS") == [False, False, True, False, False, False, False]
    assert _cells(week, "бот") == [False, False, False, True, False, True, True]
    assert "".join(f"<span>{d}</span>" for d in ["Чт", "Пт", "Сб", "Вс", "Пн", "Вт", "Ср"]) in week
    assert "Ступень 4 — Дисциплинированный" in growth and "оценка от 02.11" in growth and "250" in growth

    no_gateway = views[1][1]
    assert "Другие места недоступны" in no_gateway and '<span class="k">IWE</span>' not in no_gateway
    assert _cells(no_gateway, "бот") == [True] * 7
    assert "В других местах активности нет" in views[2][1]
    assert "No data yet" in views[3][1] and "No valid diagnostic yet" in views[3][2]
    assert _cells(views[4][1], "бот") == [True] and "<span>Пн</span>" in views[4][1]
    assert "Диагностика не пройдена или устарела — /diagnose" in views[5][2]
    assert "всего баллов" not in views[6][2]
    assert "Stage 4 — Disciplined" in views[7][2] and "Growth" in views[7][0]


def test_widget_and_text_card_show_the_same_week():
    cases = [_board(), _board(masks=None), _board(masks=[0] * 29), _board(active="1", masks=[0], start=date(2026, 11, 9))]
    views = _render_tabs([build_progress_chat_data(cache, "ru") for cache in cases])

    for cache, view in zip(cases, views):
        week = last_7_days(cache["calendar"])
        line = overview_week_line(cache["calendar"], "ru")
        places = [("IWE", "iwe"), ("клуб", "club"), ("LMS", "lms"), ("бот", "bot")]
        shown = places if week["has_places"] else places[3:]
        for label, place in shown:
            assert _count(view[1], label) == week["counts"][place]
            assert f"{label} {week['counts'][place]}" in line
