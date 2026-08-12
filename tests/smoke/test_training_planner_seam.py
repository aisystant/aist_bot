"""
Тесты: strangler-seam над training planner (WP-262 В1, Ф1).

Покрывает engines/training/port.py (TrainingPlannerPort, LocalTrainingPlanner)
и его подключение в engines/training/engine.py.

Три уровня проверки:
1. LocalTrainingPlanner — тонкая обёртка, делегирует в planner.py без
   изменения аргументов/результата (поведение сохранено).
2. TrainingEngine — оба живых call site (generate_assignment,
   generate_child_assignment) реально вызывают инжектированный порт,
   а не planner.py напрямую.
3. Статическая проверка исходника engine.py: нет прямого импорта
   generate_assignment_text/generate_child_assignment_text из planner —
   только через self._planner_port.
"""

import inspect
import re

import pytest
from unittest.mock import AsyncMock, patch

from engines.training.port import (
    LocalTrainingPlanner,
    TrainingPlannerPort,
    default_training_planner_port,
)
from engines.training.engine import TrainingEngine
from engines.training import engine as engine_module


# ─── 1. LocalTrainingPlanner делегирует в planner.py без изменения поведения ───

@pytest.mark.asyncio
async def test_local_planner_delegates_generate_assignment_text():
    port = LocalTrainingPlanner()
    cell_data = {"forms": {"postformal": "form"}, "transfer_test": "tt", "can_do": [], "domains": []}
    intern = {"language": "ru", "name": "Тест"}

    with patch(
        "engines.training.planner.generate_assignment_text", new_callable=AsyncMock
    ) as mock_planner_fn:
        mock_planner_fn.return_value = "сгенерированное задание"

        result = await port.generate_assignment_text(cell_data, "postformal", intern, "Аксиоматичность", 2)

        mock_planner_fn.assert_awaited_once_with(cell_data, "postformal", intern, "Аксиоматичность", 2)
        assert result == "сгенерированное задание"


@pytest.mark.asyncio
async def test_local_planner_delegates_generate_child_assignment_text():
    port = LocalTrainingPlanner()
    cell_data = {"forms": {}, "transfer_test": "tt", "can_do": [], "domains": []}

    with patch(
        "engines.training.planner.generate_child_assignment_text", new_callable=AsyncMock
    ) as mock_planner_fn:
        mock_planner_fn.return_value = "карточка занятия"

        result = await port.generate_child_assignment_text(
            cell_data, "concrete_operational", "Ваня", "Структура", 1,
            lang="ru", account_id="ory-uuid-789",
        )

        mock_planner_fn.assert_awaited_once_with(
            cell_data, "concrete_operational", "Ваня", "Структура", 1,
            lang="ru", account_id="ory-uuid-789",
        )
        assert result == "карточка занятия"


def test_default_port_is_local_training_planner():
    """Ф1: единственная реализация — локальная. Платформенной здесь нет."""
    assert isinstance(default_training_planner_port, LocalTrainingPlanner)
    assert isinstance(default_training_planner_port, TrainingPlannerPort)


# ─── 2. TrainingEngine реально вызывает инжектированный порт ───

class _FakePlannerPort(TrainingPlannerPort):
    """Тестовый двойник порта — не трогает planner.py/Claude вообще."""

    def __init__(self):
        self.assignment_calls = []
        self.child_calls = []

    async def generate_assignment_text(self, cell_data, cognitive_level, intern, principle_name, depth):
        self.assignment_calls.append((cell_data, cognitive_level, intern, principle_name, depth))
        return "FAKE:assignment"

    async def generate_child_assignment_text(self, cell_data, cognitive_level, child_name, principle_name, depth, lang='ru', account_id=None):
        self.child_calls.append((cell_data, cognitive_level, child_name, principle_name, depth, lang, account_id))
        return "FAKE:child_assignment"


@pytest.mark.asyncio
async def test_engine_generate_assignment_uses_injected_port():
    fake_port = _FakePlannerPort()
    engine = TrainingEngine(chat_id=12345, planner_port=fake_port)

    depth_data = {"bloom_level": "Remember", "bridge_template": "", "criteria": "", "common_errors": [], "can_do": []}
    zp_cells = {"ZP.1": {"depths": {"1": depth_data}}}

    with patch("engines.training.engine.get_training_settings", new_callable=AsyncMock) as mock_settings, \
         patch("engines.training.engine.get_principle_depth", new_callable=AsyncMock) as mock_depth, \
         patch("engines.training.engine.get_intern", new_callable=AsyncMock) as mock_intern, \
         patch("engines.training.planner.load_zp_cells", return_value=zp_cells), \
         patch("engines.training.planner.get_principle_name", return_value="Аксиоматичность"), \
         patch("clients.claude.ClaudeClient.generate", new_callable=AsyncMock) as mock_claude_generate:
        mock_settings.return_value = {"enabled_principles": ["ZP.1"], "cognitive_level": "postformal"}
        mock_depth.return_value = 0  # target_depth = 1
        mock_intern.return_value = {"language": "ru", "name": "Тест", "dt_user_id": "ory-uuid-123"}

        result = await engine.generate_assignment("ZP.1")

        assert result is not None
        assert result["assignment_text"] == "FAKE:assignment"
        assert len(fake_port.assignment_calls) == 1
        # account_id (dt_user_id из intern) дошёл до порта — атрибуция llm-proxy (AR.218, WP-262 Ф2)
        _, _, intern_arg, _, _ = fake_port.assignment_calls[0]
        assert intern_arg["dt_user_id"] == "ory-uuid-123"
        # Реальный Claude-клиент НЕ вызван — вызов ушёл строго через порт
        mock_claude_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_generate_child_assignment_uses_injected_port():
    fake_port = _FakePlannerPort()
    engine = TrainingEngine(chat_id=999, planner_port=fake_port)

    depth_data = {"bloom_level": "Remember", "criteria": "", "can_do": [], "domains": [], "forms": {}, "transfer_test": ""}
    kids_cells = {"Z0": {"depths": {"1": depth_data}}}
    child = {"chat_id": 999, "name": "Ваня", "cognitive_level": "concrete_operational"}

    with patch("engines.training.engine.get_training_child", new_callable=AsyncMock) as mock_child, \
         patch("engines.training.engine.get_child_principle_depth", new_callable=AsyncMock) as mock_depth, \
         patch("engines.training.engine.get_intern", new_callable=AsyncMock) as mock_intern, \
         patch("engines.training.planner.load_kids_cells", return_value=kids_cells), \
         patch("engines.training.planner.get_kid_principle_name", return_value="Целостность"), \
         patch("clients.claude.ClaudeClient.generate", new_callable=AsyncMock) as mock_claude_generate:
        mock_child.return_value = child
        mock_depth.return_value = 0  # target_depth = 1
        mock_intern.return_value = {"language": "ru"}

        result = await engine.generate_child_assignment(child_id=1, principle_id="Z0")

        assert result is not None
        assert result["assignment_text"] == "FAKE:child_assignment"
        assert len(fake_port.child_calls) == 1
        mock_claude_generate.assert_not_awaited()


# ─── 3. Статическая проверка: engine.py не импортирует planner-функции напрямую ───

def test_engine_source_has_no_direct_planner_generation_import():
    """Живые call sites обязаны идти через self._planner_port, не через planner.py напрямую."""
    src = inspect.getsource(engine_module)

    # Ни одна строка импорта из .planner не должна тянуть сами generation-функции
    import_lines = re.findall(r"from \.planner import [^\n]+", src)
    for line in import_lines:
        assert "generate_assignment_text" not in line, f"Прямой импорт planner-функции: {line}"
        assert "generate_child_assignment_text" not in line, f"Прямой импорт planner-функции: {line}"

    # Вызовы обязаны идти через порт
    assert "self._planner_port.generate_assignment_text(" in src
    assert "self._planner_port.generate_child_assignment_text(" in src
