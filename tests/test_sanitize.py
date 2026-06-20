"""
Tests for helpers/sanitize.py — enforce_iwe_style().

Run: python3 -m pytest tests/test_sanitize.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers.sanitize import enforce_iwe_style


def test_empty_string_returns_empty():
    assert enforce_iwe_style("") == ""


def test_no_dash_text_unchanged():
    text = "Обычный текст без тире."
    assert enforce_iwe_style(text) == text


def test_allowed_em_dash_preserved():
    # "X — это Y" is the only allowed em-dash construction
    assert enforce_iwe_style("Бот — это тонкий клиент") == "Бот — это тонкий клиент"


def test_forbidden_em_dash_replaced():
    assert enforce_iwe_style("Результат — хороший") == "Результат - хороший"


def test_mid_sentence_em_dash_replaced():
    assert enforce_iwe_style("Цель — завтра") == "Цель - завтра"


def test_multiple_em_dashes_mixed():
    # "Бот — это" preserved; "платформа — инструмент" replaced
    result = enforce_iwe_style("Бот — это клиент, платформа — инструмент")
    assert "Бот — это" in result
    assert "платформа - инструмент" in result


def test_multiple_allowed_constructions_preserved():
    result = enforce_iwe_style("Агент — это система, а роль — это место")
    assert "Агент — это" in result
    assert "роль — это" in result


def test_em_dash_at_line_start_replaced():
    # em-dash at start has no word before it → not "X — это" → replace
    # leading space is an acceptable artifact of [^\S\n]*—[^\S\n]* → " - "
    result = enforce_iwe_style("— Начало фразы")
    assert result == " - Начало фразы"


def test_multiline_newline_preserved():
    # \n between em-dash and next line must not be collapsed
    result = enforce_iwe_style("Цель —\nулучшить")
    assert "\n" in result
    assert "—" not in result


def test_placeholder_not_leaking_in_output():
    # Null byte placeholder must not appear in final output
    result = enforce_iwe_style("Бот — это клиент, цель — победить")
    assert "\x00" not in result


def test_text_with_no_forbidden_dashes_unchanged():
    text = "Агент — это система, роль — это место"
    result = enforce_iwe_style(text)
    assert "\x00" not in result
    assert "—" in result  # the allowed ones survive
