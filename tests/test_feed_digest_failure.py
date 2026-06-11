"""
Смок-тест: при провале генерации дайджест НЕ маскируется заглушкой.

Регрессия 10-11 июня 2026: при 401 на LLM-прокси generate_multi_topic_digest()
возвращала dict с main_content="Контент не удалось сгенерировать. Попробуйте
позже." — непустая строка проходила guard вызывающих и доставлялась как готовый
дайджест. Фикс: при пустом ответе Claude генератор возвращает None.

Запуск: python -m pytest tests/test_feed_digest_failure.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import engines.feed.planner as planner


INTERN = {
    "language": "ru",
    "name": "Тест",
    "occupation": "руководитель",
    "complexity_level": 1,
}
TOPICS = ["Планирование"]


@pytest.fixture
def no_mcp(monkeypatch):
    """MCP-поиск контекста отключён — тест не ходит в сеть."""
    async def _empty_search(*args, **kwargs):
        return []
    monkeypatch.setattr(planner.gateway_mcp, "search", _empty_search)


async def _gen():
    return await planner.generate_multi_topic_digest(
        topics=TOPICS, intern=INTERN, duration=8, depth_level=1
    )


async def test_digest_none_when_claude_returns_none(no_mcp, monkeypatch):
    """Пустой ответ Claude → None, а не заглушка."""
    async def _none(*a, **k):
        return None
    monkeypatch.setattr(planner.claude, "generate", _none)

    assert await _gen() is None


async def test_digest_none_when_claude_returns_blank(no_mcp, monkeypatch):
    """Пустая строка от Claude (falsy) → None."""
    async def _blank(*a, **k):
        return ""
    monkeypatch.setattr(planner.claude, "generate", _blank)

    assert await _gen() is None


async def test_digest_ok_when_claude_returns_json(no_mcp, monkeypatch):
    """Валидный JSON → нормальный дайджест с непустым main_content."""
    payload = (
        '{"intro":"Вступление",'
        '"topics":[{"title":"Планирование","summary":"суть","detail":"подробно"}],'
        '"reflection_prompt":"вопрос"}'
    )
    async def _json(*a, **k):
        return payload
    monkeypatch.setattr(planner.claude, "generate", _json)

    result = await _gen()
    assert result is not None
    assert result["main_content"]              # непустой
    assert len(result["topics_detail"]) == 1   # JSON разобран


async def test_digest_raw_text_fallback_preserved(no_mcp, monkeypatch):
    """Ответ Claude без JSON → сырой текст как контент (легитимный fallback цел)."""
    async def _raw(*a, **k):
        return "Просто текст без JSON"
    monkeypatch.setattr(planner.claude, "generate", _raw)

    result = await _gen()
    assert result is not None
    assert result["main_content"] == "Просто текст без JSON"
