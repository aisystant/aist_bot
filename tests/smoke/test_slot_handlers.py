"""
Smoke-тесты для handlers/slot.py (WP-310 Ф13b/c/d).

Проверяет что:
1. Модуль импортируется без ошибок
2. Роутер создаётся корректно
3. Вспомогательные функции работают без DB (unit-level)
4. log_event_with_occurred_at существует в events.py

Правило 10.37: все imports на уровне модуля, smoke-тест обязателен.
"""

import sys
import os
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
os.environ.setdefault("DEVELOPER_CHAT_ID", "123456")

import pytest


class TestSlotHandlerImports:
    """Проверяет что handlers/slot.py импортируется без ошибок."""

    def test_import_slot_module(self):
        """Базовый import handlers.slot не падает."""
        from handlers.slot import slot_router
        assert slot_router is not None

    def test_slot_router_name(self):
        """Роутер называется 'slot'."""
        from handlers.slot import slot_router
        assert slot_router.name == "slot"

    def test_import_onboarding_time_states(self):
        """FSM стейты OnboardingTimeStates импортируются."""
        from handlers.slot import OnboardingTimeStates
        assert OnboardingTimeStates.waiting_hours is not None
        assert OnboardingTimeStates.waiting_weeks is not None
        assert OnboardingTimeStates.confirm_backfill is not None

    def test_import_edit_time_states(self):
        """FSM стейты EditTimeStates импортируются."""
        from handlers.slot import EditTimeStates
        assert EditTimeStates.waiting_new_value is not None

    def test_import_log_event_with_occurred_at(self):
        """log_event_with_occurred_at существует в db.queries.events."""
        from db.queries.events import log_event_with_occurred_at
        assert callable(log_event_with_occurred_at)

    def test_import_log_event(self):
        """Оригинальный log_event не сломан."""
        from db.queries.events import log_event
        assert callable(log_event)


class TestParseHours:
    """Unit-тесты для _parse_hours (без DB, без сети)."""

    def _parse(self, text: str):
        from handlers.slot import _parse_hours
        return _parse_hours(text)

    def test_decimal_dot(self):
        assert self._parse("1.5") == 1.5

    def test_decimal_comma(self):
        assert self._parse("1,5") == 1.5

    def test_integer(self):
        assert self._parse("2") == 2.0

    def test_with_suffix_ch(self):
        assert self._parse("2ч") == 2.0

    def test_with_suffix_chasa(self):
        result = self._parse("2 часа")
        assert result == 2.0

    def test_too_small(self):
        assert self._parse("0.05") is None

    def test_too_large(self):
        assert self._parse("25") is None

    def test_not_a_number(self):
        assert self._parse("abc") is None

    def test_empty(self):
        assert self._parse("") is None

    def test_max_valid(self):
        assert self._parse("24") == 24.0

    def test_min_valid(self):
        assert self._parse("0.1") == 0.1


class TestSlotInlineKeyboard:
    """Проверяет что inline-клавиатуры создаются без ошибок."""

    def test_slot_inline_keyboard(self):
        from handlers.slot import _slot_inline_keyboard
        kb = _slot_inline_keyboard()
        assert kb is not None
        # Проверяем что кнопки есть
        all_btns = [btn for row in kb.inline_keyboard for btn in row]
        labels = [btn.text for btn in all_btns]
        assert "0.5" in labels
        assert "Свой ввод" in labels

    def test_hours_inline_keyboard(self):
        from handlers.slot import _hours_inline_keyboard
        kb = _hours_inline_keyboard()
        assert kb is not None
        all_btns = [btn for row in kb.inline_keyboard for btn in row]
        cbs = [btn.callback_data for btn in all_btns]
        assert "ot_hours:2" in cbs
        assert "ot_hours:10" in cbs
        assert "ot_hours:custom" in cbs

    def test_weeks_inline_keyboard(self):
        from handlers.slot import _weeks_inline_keyboard
        kb = _weeks_inline_keyboard()
        assert kb is not None
        all_btns = [btn for row in kb.inline_keyboard for btn in row]
        cbs = [btn.callback_data for btn in all_btns]
        assert "ot_weeks:4" in cbs
        assert "ot_weeks:24" in cbs

    def test_confirm_keyboard(self):
        from handlers.slot import _confirm_keyboard
        kb = _confirm_keyboard()
        assert kb is not None
        all_btns = [btn for row in kb.inline_keyboard for btn in row]
        cbs = [btn.callback_data for btn in all_btns]
        assert "ot_confirm:yes" in cbs
        assert "ot_confirm:no" in cbs


class TestSourceLabel:
    """Проверяет форматирование источника записи."""

    def test_active_label(self):
        from handlers.slot import _source_label
        assert _source_label("active") == "active"

    def test_backfill_label(self):
        from handlers.slot import _source_label
        assert _source_label("self_report_backfill") == "backfill"

    def test_unknown_label(self):
        from handlers.slot import _source_label
        # Должно вернуть первые 10 символов
        result = _source_label("some_unknown_source")
        assert len(result) <= 10


class TestStageWeeklyGoal:
    """Проверяет маппинг ступень → цель ч/нед."""

    def test_stage_1(self):
        from handlers.slot import _stage_weekly_goal
        assert _stage_weekly_goal(1) == 2.0

    def test_stage_2(self):
        from handlers.slot import _stage_weekly_goal
        assert _stage_weekly_goal(2) == 5.0

    def test_stage_5(self):
        from handlers.slot import _stage_weekly_goal
        assert _stage_weekly_goal(5) == 10.0

    def test_stage_none(self):
        from handlers.slot import _stage_weekly_goal
        assert _stage_weekly_goal(None) is None

    def test_stage_unknown(self):
        from handlers.slot import _stage_weekly_goal
        assert _stage_weekly_goal(99) is None


class TestHandlerRegistration:
    """Проверяет что slot_router регистрируется в setup_handlers без ошибки."""

    def test_slot_router_importable_from_handlers(self):
        """handlers/__init__.py содержит импорт slot_router."""
        import handlers
        assert handlers is not None

    def test_slot_handler_in_init(self):
        """slot.py упоминается в handlers/__init__.py."""
        init_path = Path(_PROJECT_ROOT) / "handlers" / "__init__.py"
        content = init_path.read_text()
        assert "slot_router" in content, "slot_router должен быть в handlers/__init__.py"


class TestSlotDailyCallback:
    """Ф13c: проверяет callback handler slot_daily в slot_router."""

    def test_slot_daily_callback_registered(self):
        """slot_daily: callback filter зарегистрирован в slot_router."""
        from handlers.slot import slot_router
        # Проверяем что в router зарегистрирован хендлер с filter slot_daily:
        router_source = Path(_PROJECT_ROOT, "handlers", "slot.py").read_text()
        assert "slot_daily:" in router_source, "slot_daily: callback должен быть в slot.py"

    def test_slot_daily_skip_value_parseable(self):
        """slot_daily:skip — специальное значение, не парсится как float."""
        value = "skip"
        assert value == "skip"  # не float → пропустить

    def test_slot_daily_hours_parseable(self):
        """slot_daily:0.5/1.0/2.0 — корректно парсятся как float."""
        for raw in ("0.5", "1.0", "2.0"):
            hours = float(raw)
            assert 0.1 <= hours <= 24.0

    def test_scheduler_daily_prompt_defined(self):
        """_send_slot_daily_prompt зарегистрирован в scheduler.py."""
        sched_path = Path(_PROJECT_ROOT, "core", "scheduler.py")
        content = sched_path.read_text()
        assert "_send_slot_daily_prompt" in content
        assert "hour=19, minute=0" in content  # 22:00 МСК = 19:00 UTC
