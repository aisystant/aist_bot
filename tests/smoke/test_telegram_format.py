"""
Unit-тесты для helpers.telegram_format._inline_format.

Покрывают защиту HTML-тегов через placeholder-pattern (\x00PH{idx}\x00)
из коммита 979e2a7, ветка pilot.

Защищённые теги (whitelist): b, i, u, s, code, pre, a, tg-spoiler, details, summary.
"""

import pytest

from helpers.telegram_format import _inline_format


# ─── Защита структурных тегов ───

def test_details_tag_preserved():
    """<details>...</details> не превращается в &lt;details&gt;."""
    result = _inline_format("<details>содержимое</details>")
    assert "<details>" in result
    assert "</details>" in result
    assert "&lt;details&gt;" not in result


def test_summary_tag_preserved():
    """<summary>...</summary> сохраняется."""
    result = _inline_format("<summary>Заголовок</summary>")
    assert "<summary>" in result
    assert "</summary>" in result
    assert "&lt;summary&gt;" not in result


def test_anchor_tag_with_href_preserved():
    """<a href="https://example.com">link</a> — тег и href сохранены."""
    result = _inline_format('<a href="https://example.com">link</a>')
    assert '<a href="https://example.com">' in result
    assert "</a>" in result
    assert "&lt;a" not in result


# ─── Все 10 защищённых тегов (open + close) ───

@pytest.mark.parametrize("tag", [
    "b", "i", "u", "s", "code", "pre", "a", "tg-spoiler", "details", "summary"
])
def test_all_whitelisted_open_tags_preserved(tag):
    """Open-тег из whitelist сохраняется без экранирования."""
    result = _inline_format(f"<{tag}>текст</{tag}>")
    assert f"<{tag}>" in result, f"<{tag}> должен сохраниться, получили: {result}"
    assert f"</{tag}>" in result, f"</{tag}> должен сохраниться, получили: {result}"


# ─── Неизвестный тег — экранируется ───

def test_unknown_tag_is_escaped():
    """<unknown>tag</unknown> — не в whitelist, экранируется."""
    result = _inline_format("<unknown>tag</unknown>")
    assert "&lt;unknown&gt;" in result
    assert "&lt;/unknown&gt;" in result
    assert "<unknown>" not in result


# ─── Амперсанд в обычном тексте ───

def test_ampersand_is_escaped():
    """Амперсанд в тексте → &amp;."""
    result = _inline_format("кофе & молоко")
    assert "&amp;" in result
    assert " & " not in result


# ─── Markdown bold конвертируется ───

def test_markdown_bold_converted():
    """**текст** → <b>текст</b>."""
    result = _inline_format("**жирный**")
    assert "<b>жирный</b>" in result
    assert "**" not in result


# ─── Markdown code конвертируется ───

def test_markdown_code_converted():
    """`code` → <code>code</code>."""
    result = _inline_format("`print()`")
    assert "<code>print()</code>" in result


# ─── Идемпотентность ───

def test_idempotency_plain_text():
    """Двойной вызов _inline_format не ломает plain text."""
    text = "Привет, мир!"
    once = _inline_format(text)
    twice = _inline_format(once)
    assert once == twice


def test_idempotency_with_html_tags():
    """Двойной вызов с HTML-тегами из whitelist — теги сохраняются оба раза."""
    text = "<b>жирный</b>"
    once = _inline_format(text)
    twice = _inline_format(once)
    # После первого вызова тег уже защищён — второй не должен его сломать
    assert "<b>" in twice
    assert "</b>" in twice


# ─── Граничный кейс: буквальный placeholder в тексте ───

def test_literal_placeholder_in_text():
    """
    Текст содержит буквальный \x00PH0\x00.
    Документируем поведение: placeholder будет заменён пустой строкой
    (нет реального тега по индексу 0 при пустом тексте вокруг),
    но функция не падает.
    """
    raw = "\x00PH0\x00текст"
    result = _inline_format(raw)
    # Функция не должна бросать исключение — это основной контракт
    assert isinstance(result, str)
