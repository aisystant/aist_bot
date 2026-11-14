"""/remind time parsing (WP-568, 14.11).

Two test reminders on 13.11 (pilot 125, production 3609) fired after 1 h 3 min
instead of 3 min: the phrase was not parsed and the +1 h default was applied
silently. These tests pin the parsed forms, the refusal for a named but
unreadable time, the explicit default and the Moscow display.
"""

import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from handlers import remind  # noqa: E402

NOW = datetime(2026, 11, 14, 10, 0, 0)  # UTC naive, = 13:00 МСК


@pytest.fixture(autouse=True)
def fixed_now():
    fake = MagicMock(wraps=datetime)
    fake.utcnow.return_value = NOW
    with patch.object(remind, "datetime", fake):
        yield


def _delta(text):
    when, rest = remind._parse_time(text)
    assert when is not None, f"not parsed: {text!r}"
    return when - NOW, " ".join(rest.split())


# Forms that already worked must keep their result.
@pytest.mark.parametrize(
    "text, delta",
    [
        ("тест через 3 минуты", timedelta(minutes=3)),
        ("тест через 3 мин", timedelta(minutes=3)),
        ("тест через 3мин", timedelta(minutes=3)),
        ("тест через 5 минут", timedelta(minutes=5)),
        ("тест через 2 часа", timedelta(hours=2)),
        ("тест через 5 часов", timedelta(hours=5)),
        ("тест через 2 дня", timedelta(days=2)),
        ("тест через 5 дней", timedelta(days=5)),
    ],
)
def test_previous_forms_unchanged(text, delta):
    assert _delta(text) == (delta, "тест")


# Forms that used to fall back to +1 h silently.
@pytest.mark.parametrize(
    "text, delta",
    [
        ("тест через минуту", timedelta(minutes=1)),
        ("тест через 1 минуту", timedelta(minutes=1)),
        ("тест через три минуты", timedelta(minutes=3)),
        ("тест через десять минут", timedelta(minutes=10)),
        ("тест через 3м", timedelta(minutes=3)),
        ("тест через полчаса", timedelta(minutes=30)),
        ("тест через полтора часа", timedelta(minutes=90)),
        ("тест через час", timedelta(hours=1)),
        ("тест через 1 час 30 минут", timedelta(hours=1, minutes=30)),
        ("тест через 2ч 15м", timedelta(hours=2, minutes=15)),
        ("тест через два часа и десять минут", timedelta(hours=2, minutes=10)),
        ("тест через день", timedelta(days=1)),
        ("тест через сутки", timedelta(days=1)),
        ("тест через неделю", timedelta(weeks=1)),
        ("тест через 2 недели", timedelta(weeks=2)),
        ("тест через 2 суток", timedelta(days=2)),
        ("тест через 2 часика", timedelta(hours=2)),
        ("тест через 5 минуток", timedelta(minutes=5)),
        ("тест через час 30 минут", timedelta(hours=1, minutes=30)),
    ],
)
def test_new_relative_forms(text, delta):
    assert _delta(text) == (delta, "тест")


def test_compound_wins_over_simple_hour():
    # The simple pattern alone would read «через 1 час» and leave «30 минут» in the text.
    assert _delta("созвон через 1 час 30 минут с командой") == (
        timedelta(hours=1, minutes=30),
        "созвон с командой",
    )


def test_unit_does_not_run_into_next_word():
    # «через 5 мест» is not «через 5 м»: no time parsed and no time named.
    when, _ = remind._parse_time("занять через 5 мест очередь")
    assert when is None
    assert not remind.has_time_intent("занять через 5 мест очередь")


@pytest.mark.parametrize(
    "text",
    [
        "тест через 2x часа",
        "тест через пару минут",
        "тест через три с половиной часа",
        "тест через месяц",
        "тест через год",
        "тест через 2 месяца",
        "тест через 30 сек",
        "тест через секунду",
        "тест через полдня",
        "тест через полчасика",
    ],
)
def test_named_but_unreadable_time_is_intent(text):
    when, _ = remind._parse_time(text)
    assert when is None
    assert remind.has_time_intent(text)


# Ordinary text with «через»: must keep the +1 h default, not be refused (review 14.11).
@pytest.mark.parametrize(
    "text",
    [
        "позвонить через Васю",
        "купить хлеб",
        "передать через охрану",
        "пройти через парк",
        "выйти через дверь",
        "передать через семью",
        "позвонить через Трифонова",
        "передать через Дениса",
        "записаться через поликлинику",
        "передать документы через полицию",
        "пройти через Часовую улицу",
        "передать через дневник",
        "оплатить через 1С",
        "заказать через 2ГИС",
        "войти через 3 подъезд",
        "пройти через пол",
    ],
)
def test_no_time_named(text):
    when, _ = remind._parse_time(text)
    assert when is None
    assert not remind.has_time_intent(text)


def test_moscow_display():
    assert remind._format_dt(datetime(2026, 11, 14, 10, 30)) == "14.11.2026 13:30 МСК"


def _message(text):
    msg = SimpleNamespace(
        chat=SimpleNamespace(id=42),
        text=text,
        bot=SimpleNamespace(id=7),
        answer=AsyncMock(),
    )
    return msg


def _pool_with_row(row_id=501):
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": row_id})
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    return pool, conn


@pytest.mark.asyncio
async def test_unreadable_time_refuses_and_creates_nothing():
    msg = _message("/remind тест через 2x часа")
    pool, conn = _pool_with_row()
    with patch("db.connection.get_learning_pool", AsyncMock(return_value=pool)):
        await remind.cmd_remind(msg)
    conn.fetchrow.assert_not_awaited()
    msg.answer.assert_awaited_once_with(remind.UNPARSED_TIME_REPLY)


@pytest.mark.asyncio
async def test_default_hour_is_stated_in_confirmation():
    msg = _message("/remind купить хлеб")
    pool, conn = _pool_with_row()
    with patch("db.connection.get_learning_pool", AsyncMock(return_value=pool)):
        await remind.cmd_remind(msg)
    scheduled = conn.fetchrow.await_args.args[2]
    assert scheduled == NOW + timedelta(hours=1)
    reply = msg.answer.await_args.args[0]
    assert "14.11.2026 14:00 МСК — через 1 час (время не указано)" in reply
    assert "UTC" not in reply


@pytest.mark.asyncio
async def test_parsed_time_confirmation_in_moscow():
    msg = _message("/remind тест через 3 минуты")
    pool, conn = _pool_with_row()
    with patch("db.connection.get_learning_pool", AsyncMock(return_value=pool)):
        await remind.cmd_remind(msg)
    assert conn.fetchrow.await_args.args[2] == NOW + timedelta(minutes=3)
    reply = msg.answer.await_args.args[0]
    assert "«тест»" in reply
    assert "14.11.2026 13:03 МСК\n" in reply
    assert remind.DEFAULT_TIME_NOTE not in reply
