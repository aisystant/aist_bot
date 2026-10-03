"""
parse_amount() — нормализация цены из Aisystant API (WP-5).

Инцидент-триггер: /buy показывал 5 программ вместо 7 (сверка с @SystemsSchool_bot,
07.09.2026, peer-session 2026-09-07-07-aist-seminar-showcase). Часть фикса —
защита от разных представлений цены (число, строка, с пробелами/NBSP), т.к.
точный формат ответа Aisystant не подтверждён живым запросом.
"""

import pytest

from clients.aisystant import parse_amount


@pytest.mark.parametrize(
    "raw,expected",
    [
        (60000, 60000.0),
        (60000.0, 60000.0),
        ("60000", 60000.0),
        ("60000.00", 60000.0),
        ("60 000", 60000.0),
        ("60 000", 60000.0),  # NBSP-разделитель разрядов
        (None, 0.0),
        ("", 0.0),
        ("invalid", 0.0),
        (0, 0.0),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected
