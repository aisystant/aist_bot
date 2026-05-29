"""
Unit-тесты detect_onboarding_intent (WP-349 Ф22).

Чистая функция — без БД, без mock, без aiogram.
"""

import pytest

from config.onboarding_intents import ONBOARDING_INTENT_MAP, NEGATIVE_PATTERNS
from handlers.onboarding_intent import detect_onboarding_intent


# ═══════════════════════════════════════════════════════════════════════════
# 1. Positive matches
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,expected", [
    # --- marathon ---
    ("начать марафон", "marathon"),
    ("хочу учиться", "marathon"),
    ("начать обучение", "marathon"),
    ("первый урок", "marathon"),
    # --- diagnose ---
    ("узнать ступень", "diagnose"),
    ("диагностируй", "diagnose"),
    ("куда учиться", "diagnose"),
    ("моя диагностика", "diagnose"),
    # --- setup ---
    ("обзор платформы", "setup"),
    ("как настроить", "setup"),
    ("возможности платформы", "setup"),
    ("что умеет платформа", "setup"),
])
def test_positive_match(text, expected):
    """Каждый intent распознаётся по своим ключевым фразам."""
    assert detect_onboarding_intent(text) == expected


# ═══════════════════════════════════════════════════════════════════════════
# 2. Negative patterns → None
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text", [
    "не хочу марафон",
    "не нужна диагностика",
    "не интересно обучение",
    "не надо платформу",
    "не готов учиться",
    "не хочется узнать ступень",
])
def test_negative_patterns(text):
    """Отрицание любого интента → None, независимо от keywords."""
    assert detect_onboarding_intent(text) is None


# ═══════════════════════════════════════════════════════════════════════════
# 3. Word-boundary protection
# ═══════════════════════════════════════════════════════════════════════════

def test_word_boundary_inside_word():
    """Ключевое слово внутри другого слова → None (марафонский)."""
    assert detect_onboarding_intent("марафонский бег") is None
    assert detect_onboarding_intent("диагностический тест") is None


def test_keyword_requires_space_not_hyphen():
    """Дефис не является word-char, но keyword 'начать марафон' требует пробел.
    'начать-марафон' → None (keyword mismatch, не word-boundary issue).
    """
    assert detect_onboarding_intent("начать-марафон") is None


# ═══════════════════════════════════════════════════════════════════════════
# 4. Punctuation / case / whitespace
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,expected", [
    ("  Начать МАРАФОН!  ", "marathon"),
    ("узнать ступень?", "diagnose"),
    ("ОБЗОР ПЛАТФОРМЫ.", "setup"),
    ("\n\tначать занятия\n", "marathon"),
])
def test_punctuation_case_whitespace(text, expected):
    """Регистронезависимость, trim, знаки препинания."""
    assert detect_onboarding_intent(text) == expected


# ═══════════════════════════════════════════════════════════════════════════
# 5. Multi-intent / порядок ключей в dict
# ═══════════════════════════════════════════════════════════════════════════

def test_multi_intent_map_order_wins():
    """Порядок ключей в ONBOARDING_INTENT_MAP определяет приоритет,
    не позиция фразы в тексте.
    """
    # Python 3.7+ гарантирует insertion order для dict.
    # diagnose-фраза стоит раньше в тексте, но marathon — первый ключ.
    text = "начать марафон и узнать ступень"
    assert detect_onboarding_intent(text) == "marathon"


# ═══════════════════════════════════════════════════════════════════════════
# 6. Empty / no-match + частичное отрицание
# ═══════════════════════════════════════════════════════════════════════════

def test_none_input():
    """None-вход (например, удалённое сообщение) → None без AttributeError."""
    assert detect_onboarding_intent(None) is None


def test_empty_and_random():
    """Пустая строка, whitespace-only и рандомный текст → None."""
    assert detect_onboarding_intent("") is None
    assert detect_onboarding_intent("   ") is None
    assert detect_onboarding_intent("погода сегодня") is None
    assert detect_onboarding_intent("12345 !!!") is None


def test_partial_negation_mvp_scope():
    """Частичное отрицание ('не сейчас') — намеренно игнорируется в MVP.
    NEGATIVE_PATTERNS ловит только прямой отказ.
    """
    assert detect_onboarding_intent("начать марафон, но не сейчас") == "marathon"


# ═══════════════════════════════════════════════════════════════════════════
# 7. Invariants (структурные проверки кода)
# ═══════════════════════════════════════════════════════════════════════════

def test_intent_map_keys_match_expected():
    """Убеждаемся что dict содержит ровно 3 ключа без сюрпризов."""
    expected = {"marathon", "diagnose", "setup"}
    actual = set(ONBOARDING_INTENT_MAP.keys())
    assert actual == expected, f"ONBOARDING_INTENT_MAP keys mismatch: got {actual}"


# test_negative_patterns_is_list_of_strings удалён по peer-review (Claude):
# это тест структуры конфига, не поведения функции.
