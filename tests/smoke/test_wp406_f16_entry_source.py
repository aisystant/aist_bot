"""
WP-406 Ф16-B3 (MVP): метка источника входа в событиях онбординга.

События onboarding_started / x2_completed / x3_completed / onboarding_completed
несут поле source со значениями site | stand | bot | guide-kit (дефолт bot).
Источник — deep-link `/start src_<value>`, хранение — current_context['onboarding']
['entry_source'] (по аналогии с entry_type, не заменяя его). В payload — только
значения полей, никакого PII (FORBIDDEN_FIELDS).
"""

from datetime import datetime

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.onboarder import DEFAULT_ENTRY_SOURCE, ENTRY_SOURCES, normalize_entry_source

_DT = datetime(2026, 8, 8, 12, 0, 0)


# ─────────────────────────── normalize_entry_source ───────────────────────────

def test_normalize_known_sources():
    for value in ENTRY_SOURCES:
        assert normalize_entry_source(value) == value


def test_normalize_underscore_and_case_variants():
    assert normalize_entry_source("guide_kit") == "guide-kit"
    assert normalize_entry_source("SITE") == "site"
    assert normalize_entry_source(" Stand ") == "stand"


def test_normalize_unknown_falls_back_to_bot():
    assert DEFAULT_ENTRY_SOURCE == "bot"
    assert normalize_entry_source("unknown-channel") == "bot"
    assert normalize_entry_source("") == "bot"
    assert normalize_entry_source(None) == "bot"
    assert normalize_entry_source(42) == "bot"


# ─────────────────────────── deep-link /start src_<value> ───────────────────────────

@pytest.mark.asyncio
async def test_deeplink_saves_normalized_entry_source():
    """`/start src_site` → entry_source сохраняется каноническим writer'ом Онбордера."""
    from handlers.onboarding import _save_entry_source_from_deeplink

    intern = {"current_context": {}}
    with patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_source": "site"}) as mock_save:
        await _save_entry_source_from_deeplink(111, "site", intern)

    mock_save.assert_awaited_once_with(111, {"entry_source": "site"})
    # Локальная копия intern обновлена — последующие update_intern по stale
    # current_context в cmd_start не затирают отметку.
    assert intern["current_context"]["onboarding"] == {"entry_source": "site"}


@pytest.mark.asyncio
async def test_deeplink_unknown_source_normalized_to_bot():
    from handlers.onboarding import _save_entry_source_from_deeplink

    with patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_source": "bot"}) as mock_save:
        await _save_entry_source_from_deeplink(222, "evil<script>", {"current_context": {}})

    mock_save.assert_awaited_once_with(222, {"entry_source": "bot"})


@pytest.mark.asyncio
async def test_deeplink_save_error_is_fail_open():
    """Ошибка сохранения не пробрасывается — /start продолжается."""
    from handlers.onboarding import _save_entry_source_from_deeplink

    with patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock,
               side_effect=RuntimeError("db down")):
        await _save_entry_source_from_deeplink(333, "site", {"current_context": {}})
    # Дошли сюда без исключения — fail-open соблюдён.


# ─────────────────────────── события несут source ───────────────────────────

@pytest.mark.asyncio
async def test_onboarding_started_carries_source():
    """handle() кладёт source в payload onboarding_started и фиксирует его в контексте."""
    message = AsyncMock()
    intern = {"chat_id": 317106357, "language": "ru"}

    with patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_source": "guide-kit"}), \
         patch("core.onboarder.storage.save_onboarding_context",
               new_callable=AsyncMock) as mock_save, \
         patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": True, "x3_done": True}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log, \
         patch("db.queries.onboarding_journey.get_cohort_id_for_chat",
               new_callable=AsyncMock, return_value=None):
        from core.onboarder import handle
        await handle(intern, message)

    started_call = next(c for c in mock_log.await_args_list if c.args[1] == "onboarding_started")
    assert started_call.args[2]["source"] == "guide-kit"
    assert started_call.args[2]["entry_type"] == "direct"  # entry_type не сломан
    saved_patch = mock_save.await_args_list[0].args[1]
    assert saved_patch["entry_source"] == "guide-kit"


@pytest.mark.asyncio
async def test_onboarding_started_default_source_bot():
    """Без deep-link'а source = bot (дефолт)."""
    message = AsyncMock()
    intern = {"chat_id": 317106357, "language": "ru"}

    with patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={}), \
         patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock), \
         patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": True, "x3_done": True}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log, \
         patch("db.queries.onboarding_journey.get_cohort_id_for_chat",
               new_callable=AsyncMock, return_value=None):
        from core.onboarder import handle
        await handle(intern, message)

    started_call = next(c for c in mock_log.await_args_list if c.args[1] == "onboarding_started")
    assert started_call.args[2]["source"] == "bot"


@pytest.mark.asyncio
async def test_x2_events_carry_source():
    """x2_completed и onboarding_completed из финиша Х2 несут source из контекста."""
    bot = AsyncMock()
    chat_id = 317106357

    with patch("core.onboarder.storage.get_status", new_callable=AsyncMock,
               return_value={"x2_done": True, "x3_done": True}), \
         patch("core.onboarder.storage.mark_x2_done", new_callable=AsyncMock,
               return_value={"newly_marked": True,
                             "x2_completed_at": _DT, "x3_completed_at": _DT}), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct", "entry_source": "stand"}), \
         patch("db.queries.users.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log, \
         patch("db.queries.dt_sync.ensure_default_qualification", new_callable=AsyncMock):
        from core.onboarder.x2 import _finish_x2
        await _finish_x2(bot, chat_id)

    by_type = {c.args[1]: c.args[2] for c in mock_log.await_args_list}
    assert by_type["x2_completed"]["source"] == "stand"
    assert by_type["onboarding_completed"]["source"] == "stand"
    assert by_type["x2_completed"]["entry_type"] == "direct"  # entry_type не сломан


@pytest.mark.asyncio
async def test_x3_events_carry_source():
    """x3_completed и onboarding_completed из подтверждения Х3 несут source из контекста."""
    callback = AsyncMock()
    callback.data = "x3_confirm:marathon:0"
    callback.from_user = MagicMock(id=317106357)
    callback.message = AsyncMock()

    with patch("core.onboarder.storage.mark_x3_done", new_callable=AsyncMock,
               return_value={"newly_marked": True,
                             "x2_completed_at": _DT, "x3_completed_at": _DT}), \
         patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct", "entry_source": "site"}), \
         patch("handlers.onboarding.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log, \
         patch("db.queries.dt_sync.ensure_default_qualification", new_callable=AsyncMock):
        from handlers.onboarding import on_x3_confirm
        await on_x3_confirm(callback)

    by_type = {c.args[1]: c.args[2] for c in mock_log.await_args_list}
    assert by_type["x3_completed"]["source"] == "site"
    assert by_type["onboarding_completed"]["source"] == "site"


@pytest.mark.asyncio
async def test_x3_events_default_source_bot():
    """Контекст без entry_source → события несут source=bot."""
    callback = AsyncMock()
    callback.data = "x3_confirm:marathon:0"
    callback.from_user = MagicMock(id=317106357)
    callback.message = AsyncMock()

    with patch("core.onboarder.storage.mark_x3_done", new_callable=AsyncMock,
               return_value={"newly_marked": True,
                             "x2_completed_at": _DT, "x3_completed_at": _DT}), \
         patch("core.onboarder.storage.save_onboarding_context", new_callable=AsyncMock), \
         patch("core.onboarder.storage.get_onboarding_context", new_callable=AsyncMock,
               return_value={"entry_type": "direct"}), \
         patch("handlers.onboarding.get_intern", new_callable=AsyncMock,
               return_value={"language": "ru"}), \
         patch("db.queries.events.log_event", new_callable=AsyncMock) as mock_log, \
         patch("db.queries.dt_sync.ensure_default_qualification", new_callable=AsyncMock):
        from handlers.onboarding import on_x3_confirm
        await on_x3_confirm(callback)

    by_type = {c.args[1]: c.args[2] for c in mock_log.await_args_list}
    assert by_type["x3_completed"]["source"] == "bot"
    assert by_type["onboarding_completed"]["source"] == "bot"


def test_event_payload_source_values_are_enum_only():
    """PII-guard: source в payload — только фиксированные значения, не свободный текст."""
    assert set(ENTRY_SOURCES) == {"site", "stand", "bot", "guide-kit"}
    # Любой произвольный ввод нормализуется в допустимое значение
    for raw in ("tg://user?id=1", "user@example.com", "8-900-000-00-00", "src_x"):
        assert normalize_entry_source(raw) in ENTRY_SOURCES
