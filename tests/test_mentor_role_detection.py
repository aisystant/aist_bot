"""Unit-тесты маршрутизации роли «Наставник» в _detect_role() (WP-498 Ф5).

Наставник (MIM.R.001, Режим 2) — четвёртая роль в существующем механизме
DP.D.044 (Навигатор/Диагност), маршрутизация зеркалирует тот же двухшаговый
принцип: явный префикс (приоритет) → автодетект по лексике состояния
(fallback), решение пилота 2026-07-25.

Покрывает: явный префикс (RU/EN), автодетект по паттернам, отсутствие
ложных срабатываний на нейтральных вопросах, приоритет узких паттернов
Навигатора/Диагноста над более широкими паттернами Наставника при
пересечении лексики.
"""

import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from states.common.consultation import _detect_role  # noqa: E402


# --- Явный префикс (приоритет) ---
@pytest.mark.parametrize("q", [
    "Наставник, привет",
    "наставник, помоги мне разобраться",
    "Наставник помоги",
    "Mentor, help me",
    "mentor help me please",
])
def test_explicit_mentor_prefix_detected(q):
    assert _detect_role(q) == "mentor"


# --- Автодетект по лексике состояния (fallback, без префикса) ---
@pytest.mark.parametrize("q", [
    "я застрял на задании",
    "застряла и не понимаю куда двигаться",
    "не знаю, что делать дальше",
    "не знаю что делать дальше",
    "у меня упала мотивация совсем",
    "потерял мотивацию, ничего не хочется",
    "нет мотивации вообще",
    "чувствую что в тупике",
])
def test_mentor_pattern_autodetect(q):
    assert _detect_role(q) == "mentor"


# --- Существующие роли не сломаны (регресс) ---
def test_navigator_prefix_still_works():
    assert _detect_role("Навигатор, с чего начать?") == "navigator"


def test_diagnostician_prefix_still_works():
    assert _detect_role("Диагност, определи мою ступень") == "diagnostician"


def test_navigator_pattern_routes_to_mentor_without_explicit_name():
    # WP-498 Ф14 (05.09): без явного имени вся лексика ведёт к Наставнику —
    # он сам решает внутри диспетчер-промпта, нужен ли компонент Навигатора.
    assert _detect_role("с чего мне начать учиться?") == "mentor"


def test_diagnostician_pattern_routes_to_mentor_without_explicit_name():
    # WP-498 Ф14 (05.09): аналогично для лексики Диагноста без явного имени.
    assert _detect_role("протестируй меня") == "mentor"


# --- Приоритет: явный префикс > автодетект (для Наставника тоже) ---
def test_mentor_prefix_wins_over_navigator_pattern_in_same_message():
    # Сообщение начинается с явного префикса "наставник" — не должно
    # уйти в Навигатора, даже если далее в тексте навигаторская лексика.
    assert _detect_role("наставник, с чего мне начать?") == "mentor"


# --- Порядок проверки списков (diagnostician → navigator → mentor) больше не
# влияет на итоговую роль (Ф14: все content-пути ведут к mentor) — тест
# сохранён, чтобы подтвердить отсутствие ошибки при пересечении списков.
def test_navigator_narrow_pattern_still_resolves_to_mentor():
    assert _detect_role("у меня не получается учиться совсем") == "mentor"


# --- Отсутствие ложных срабатываний на нейтральных вопросах ---
@pytest.mark.parametrize("q", [
    "какая погода в Москве",
    "привет, как дела?",
    "",
    "расскажи про системное мышление",
])
def test_no_false_positive_on_neutral_questions(q):
    assert _detect_role(q) is None
