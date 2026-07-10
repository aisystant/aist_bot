"""Unit-тесты детерминированного ответа из реестра РП на пути T4-full (WP-411 Ф7).

Покрывают три исхода `_answer_wp_registry` (не подключён / пусто / есть активные)
плюс гейт `is_wp_query` — он решает, перехватывать ли вопрос у Гермеса.
Источник регресса: прошлый фикс жил в пути консультанта (T1-T3), а T4-full в
fallback.py уходит в hermes_chat. Эти тесты ловят «фикс не в том пути».
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clients.github_oauth as github_oauth_mod
import engines.shared.consultation_tools as consultation_tools_mod
from handlers.fallback import _answer_wp_registry
from engines.shared.wp_query_detector import is_wp_query


class _FakeMessage:
    """Минимальный двойник aiogram Message — запоминает отправленный текст."""

    def __init__(self):
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


async def test_no_strategy_repo_says_not_connected(monkeypatch):
    monkeypatch.setattr(
        github_oauth_mod.github_oauth, "get_strategy_repo", AsyncMock(return_value=None)
    )
    get_wp = AsyncMock(return_value="неважно")
    monkeypatch.setattr(consultation_tools_mod, "get_wp_registry", get_wp)

    msg = _FakeMessage()
    await _answer_wp_registry(msg, chat_id=42)

    assert len(msg.answers) == 1
    assert "не подключён" in msg.answers[0]
    get_wp.assert_not_awaited()  # без репо реестр не запрашиваем


async def test_empty_registry_says_no_active(monkeypatch):
    monkeypatch.setattr(
        github_oauth_mod.github_oauth,
        "get_strategy_repo",
        AsyncMock(return_value="user/DS-my-strategy"),
    )
    monkeypatch.setattr(
        consultation_tools_mod, "get_wp_registry", AsyncMock(return_value="")
    )

    msg = _FakeMessage()
    await _answer_wp_registry(msg, chat_id=42)

    assert len(msg.answers) == 1
    # пусто ≠ нет доступа: явно нейтральная формулировка
    assert "Не нашёл активных" in msg.answers[0]
    assert "не подключён" not in msg.answers[0]


async def test_non_empty_registry_returns_list(monkeypatch):
    registry = "Активных рабочих продуктов: 2\nРП411 (P1, 🔄): Идентичность"
    monkeypatch.setattr(
        github_oauth_mod.github_oauth,
        "get_strategy_repo",
        AsyncMock(return_value="user/DS-my-strategy"),
    )
    monkeypatch.setattr(
        consultation_tools_mod, "get_wp_registry", AsyncMock(return_value=registry)
    )

    msg = _FakeMessage()
    await _answer_wp_registry(msg, chat_id=42)

    assert len(msg.answers) == 1
    assert "РП411" in msg.answers[0]
    assert "Спроси детали" in msg.answers[0]  # приглашение продолжить в Гермесе


def test_gate_intercepts_my_wp_but_not_thematic():
    # T4 перехватывает личный запрос (вкл. реальный «какие мои активные рп»),
    # но НЕ тематический и НЕ безличный — те уходят в Гермес.
    assert is_wp_query("какие мои активные рп?") is True   # дословно из инцидента
    assert is_wp_query("какие мои рп") is True
    assert is_wp_query("что такое рп") is False
    assert is_wp_query("посоветуй активные рп") is False    # нет личного сигнала
