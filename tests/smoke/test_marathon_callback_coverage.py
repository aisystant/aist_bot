"""
Regression test: все marathon callback_data в проекте должны быть покрыты handler'ом.

Проблема (2026-05-26): `cb_marathon_actions` ловил только `marathon_get_*` и
`marathon_catchup_*`. In-state callbacks (`marathon_next_question`, etc.)
проваливались сквозь щели — кнопки не работали.

Этот тест сканирует код на `callback_data="marathon_*"` и проверяет,
что каждый такой callback покрыт либо mode_router, либо callbacks_router.
"""

import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CALLBACKS_PY = PROJECT_ROOT / "handlers" / "callbacks.py"
MODE_SELECTOR_PY = PROJECT_ROOT / "engines" / "mode_selector.py"
MARATHON_STATES_DIR = PROJECT_ROOT / "states" / "workshops" / "marathon"


def _extract_marathon_callbacks(file_path: Path) -> set[str]:
    """Найти все строковые литералы callback_data="marathon_*" в файле."""
    if not file_path.exists():
        return set()
    text = file_path.read_text(encoding="utf-8")
    return set(re.findall(r'callback_data="(marathon_[^"]+)"', text))


def _extract_handler_patterns(file_path: Path) -> list[str]:
    """Извлечь паттерны обработки из aiogram handlers (F.data == / startswith)."""
    text = file_path.read_text(encoding="utf-8")
    patterns = []
    # F.data == "marathon_something"
    patterns.extend(re.findall(r'F\.data\s*==\s*"(marathon_[^"]+)"', text))
    # F.data.startswith("marathon_something")
    patterns.extend(re.findall(r'F\.data\.startswith\("(marathon_[^"]*)"\)', text))
    # data in ("marathon_a", "marathon_b")
    patterns.extend(re.findall(r'data\s+in\s*\([^)]*"(marathon_[^"]*)"[^)]*\)', text, re.DOTALL))
    return patterns


def _pattern_covers(callback: str, pattern: str) -> bool:
    """Проверить, что паттерн покрывает конкретный callback."""
    if pattern.endswith("_"):
        # startswith prefix
        return callback.startswith(pattern)
    return callback == pattern


@pytest.fixture
def known_mode_selector_callbacks() -> set[str]:
    """Callbacks, которые обрабатываются mode_router (имеет приоритет)."""
    return _extract_marathon_callbacks(MODE_SELECTOR_PY)


@pytest.fixture
def mode_selector_patterns() -> list[str]:
    return _extract_handler_patterns(MODE_SELECTOR_PY)


@pytest.fixture
def callbacks_patterns() -> list[str]:
    return _extract_handler_patterns(CALLBACKS_PY)


def test_marathon_state_callbacks_are_covered(
    known_mode_selector_callbacks,
    mode_selector_patterns,
    callbacks_patterns,
):
    """
    Каждый callback_data из marathon-стейтов должен быть покрыт:
    - либо mode_router (прямое совпадение или startswith),
    - либо callbacks_router (через cb_marathon_actions).
    """
    # Собираем все marathon callbacks из стейтов
    state_callbacks: set[str] = set()
    for py_file in MARATHON_STATES_DIR.glob("*.py"):
        state_callbacks |= _extract_marathon_callbacks(py_file)

    # Объединяем паттерны обоих роутеров
    all_patterns = mode_selector_patterns + callbacks_patterns

    uncovered = []
    for cb in state_callbacks:
        # Исключаем те, что явно обрабатываются mode_selector.py
        if cb in known_mode_selector_callbacks:
            continue
        if any(_pattern_covers(cb, pat) for pat in all_patterns):
            continue
        uncovered.append(cb)

    assert not uncovered, (
        f"Следующие marathon callbacks из states/workshops/marathon "
        f"не покрыты ни mode_router, ни callbacks_router: {uncovered}\n"
        f"Если добавляешь новый in-state callback — убедись, что он либо "
        f"обрабатывается mode_router, либо попадает под фильтр "
        f"cb_marathon_actions (F.data.startswith('marathon_'))."
    )


def test_cb_marathon_actions_has_broad_filter():
    """
    cb_marathon_actions ДОЛЖЕН иметь широкий фильтр F.data.startswith("marathon_").
    Узкий фильтр (только marathon_get_ / marathon_catchup_) ломает in-state callbacks.
    """
    text = CALLBACKS_PY.read_text(encoding="utf-8")
    assert 'F.data.startswith("marathon_")' in text, (
        "cb_marathon_actions потерял широкий фильтр F.data.startswith('marathon_'). "
        "Верни его, иначе in-state callbacks (next_question, next_bonus и др.) "
        "снова перестанут работать."
    )
