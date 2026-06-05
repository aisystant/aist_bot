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
SCHEDULER_PY = PROJECT_ROOT / "core" / "scheduler.py"
ONBOARDING_PY = PROJECT_ROOT / "handlers" / "onboarding.py"

# Источники push-кнопок: доставляются вне SM-стейта (scheduler-напоминания,
# онбординг). Их callback'и обязаны иметь явный handler у роутера, который
# реально срабатывает (а не падает в else→SM у cb_marathon_actions).
PUSH_SOURCES = [SCHEDULER_PY, ONBOARDING_PY]


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
    if pattern.endswith("_") or pattern.endswith(":"):
        # startswith prefix (marathon_diff_, marathon_checkin:, marathon_practice:)
        return callback.startswith(pattern)
    return callback == pattern


def _extract_pushed_callbacks(file_path: Path) -> set[str]:
    """Извлечь callback_data из push-кнопок (literal И f-string).

    Push-кнопки строит scheduler и доставляет вне SM-стейта — пользователь может
    кликнуть, находясь в mode_select/idle. Для f-string берём префикс до первой
    подстановки `{...}` (это и есть startswith-ключ роутинга).
    """
    if not file_path.exists():
        return set()
    text = file_path.read_text(encoding="utf-8")
    found: set[str] = set()
    # literal: callback_data="marathon_..."
    found |= set(re.findall(r'callback_data="(marathon_[^"]+)"', text))
    # f-string: callback_data=f"marathon_...{...}" → префикс до первого '{'
    for raw in re.findall(r'callback_data=f"(marathon_[^"]*)"', text):
        found.add(raw.split("{")[0])
    return found


def _extract_explicit_branches(file_path: Path) -> list[str]:
    """Явные ветки внутри cb_marathon_actions (НЕ широкий catch-all).

    Намеренно НЕ ловим `F.data.startswith("marathon_")` — это catch-all декоратор
    роутера, после которого callback падает в else→SM. Push-кнопка обязана иметь
    отдельную ветку `data == ...` / `data in (...)` / `data.startswith(...)`.
    """
    text = file_path.read_text(encoding="utf-8")
    patterns: list[str] = []
    # data in ("a", "b", ...)
    for tup in re.findall(r'\bdata\s+in\s*\(([^)]*)\)', text, re.DOTALL):
        patterns.extend(re.findall(r'"(marathon_[^"]+)"', tup))
    # bare data == "marathon_..."
    patterns.extend(re.findall(r'\bdata\s*==\s*"(marathon_[^"]+)"', text))
    # bare data.startswith("marathon_...")  (без F. — это ветка тела функции)
    patterns.extend(re.findall(r'(?<!\.)\bdata\.startswith\("(marathon_[^"]+)"\)', text))
    return patterns


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


def test_pushed_marathon_callbacks_have_explicit_branch():
    """
    Push-кнопки (scheduler доставляет вне SM-стейта) ДОЛЖНЫ покрываться ЯВНОЙ
    веткой cb_marathon_actions или mode_router — НЕ широким catch-all
    F.data.startswith("marathon_") с провалом в else→route_callback→SM.

    Регрессия 2026-06-05: marathon_practice:{day} строился f-string'ом в
    core/scheduler.py, перехватывался широким фильтром callbacks_router, падал в
    else→SM. В SM нет handler'а для marathon_practice: → callback не отвечался →
    кнопка «✏️ Перейти к практике» бесконечно мигала. Старый тест это НЕ ловил:
    сканировал только states/ + mode_selector и считал «covered» сам факт попадания
    под широкий фильтр.
    """
    pushed: set[str] = set()
    for src in PUSH_SOURCES:
        pushed |= _extract_pushed_callbacks(src)
    assert pushed, "Не найдено ни одной push-кнопки — проверь регекс/пути PUSH_SOURCES."

    # Реальное покрытие дают только роутеры, зарегистрированные ДО callbacks_router
    # (mode_router, onboarding_router) и явные ветки cb_marathon_actions. Декораторы
    # marathon_router намеренно НЕ считаем — они затенены callbacks_router.
    explicit = _extract_explicit_branches(CALLBACKS_PY)
    explicit += _extract_handler_patterns(MODE_SELECTOR_PY)
    explicit += _extract_handler_patterns(ONBOARDING_PY)

    uncovered = [
        cb for cb in sorted(pushed)
        if not any(_pattern_covers(cb, pat) for pat in explicit)
    ]

    assert not uncovered, (
        f"Push-кнопки scheduler'а без ЯВНОЙ ветки обработки: {uncovered}\n"
        f"Такой callback попадает под широкий F.data.startswith('marathon_') в "
        f"cb_marathon_actions и проваливается в else→SM. Если пользователь кликает "
        f"вне соответствующего SM-стейта — callback не отвечается, кнопка мигает.\n"
        f"Добавь ветку `elif data.startswith(\"{uncovered[0]}\"): ...` в "
        f"cb_marathon_actions (handlers/callbacks.py) с явным forward в handler."
    )
