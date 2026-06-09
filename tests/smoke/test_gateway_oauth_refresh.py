"""
Smoke-тесты: WP-7 GTW5 — Gateway OAuth refresh (cache hit + cache miss paths).

Критерии приёмки:
- cache hit: токен живой в кэше → refresh возвращает True без сетевых вызовов
- cache miss + successful refresh: токена нет → defensive reload из БД → refresh → True
- cache miss + failed refresh: токена нет → defensive reload → refresh вернул None → False
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from clients.gateway_mcp import GatewayMCPClient


@pytest.fixture
def client():
    return GatewayMCPClient(url="https://fake-gateway.test/mcp")


@pytest.fixture
def fake_token_row():
    return {
        "access_token": "old_access_123",
        "refresh_token": "old_refresh_456",
        "expires_at": datetime.utcnow() + timedelta(hours=1),
        "ory_id": "ory_test_789",
    }


@pytest.fixture
def refreshed_token_result():
    return {
        "access_token": "new_access_abc",
        "refresh_token": "new_refresh_def",
        "expires_at": datetime.utcnow() + timedelta(hours=1),
        "ory_id": "ory_test_789",
        "from_cache": False,
    }


# ─── GTW5: cache hit ───

@pytest.mark.asyncio
async def test_cache_hit(client, refreshed_token_result):
    """Токен в кэше → refresh_ory_token_with_lock вернёт from_cache=True → True."""
    chat_id = 12345
    client._tokens[chat_id] = {
        "access_token": "cached_access",
        "refresh_token": "cached_refresh",
        "expires_at": datetime.utcnow() - timedelta(minutes=5),  # expired, trigger refresh path
        "ory_id": "ory_123",
    }

    # from_cache=True означает: другой writer уже обновил токен в БД
    result_from_cache = {**refreshed_token_result, "from_cache": True}

    with patch(
        "db.queries.ory_tokens.refresh_ory_token_with_lock",
        new_callable=AsyncMock,
        return_value=result_from_cache,
    ):
        ok = await client._refresh_single_token(chat_id)

    assert ok is True
    assert client._tokens[chat_id]["access_token"] == "new_access_abc"
    assert client._tokens[chat_id]["refresh_token"] == "new_refresh_def"


# ─── GTW5: cache miss + successful refresh ───

@pytest.mark.asyncio
async def test_cache_miss_successful_refresh(client, fake_token_row, refreshed_token_result):
    """Кэш пустой → load_one_ory_token загружает из БД → refresh успешен → True."""
    chat_id = 12345
    assert chat_id not in client._tokens

    with patch(
        "db.queries.ory_tokens.load_one_ory_token",
        new_callable=AsyncMock,
        return_value=fake_token_row,
    ), patch(
        "db.queries.ory_tokens.refresh_ory_token_with_lock",
        new_callable=AsyncMock,
        return_value=refreshed_token_result,
    ):
        ok = await client._refresh_single_token(chat_id)

    assert ok is True
    assert chat_id in client._tokens
    assert client._tokens[chat_id]["access_token"] == "new_access_abc"


# ─── GTW5: cache miss + defensive reload but refresh returns None ───

@pytest.mark.asyncio
async def test_cache_miss_defensive_reload_refresh_fails(client, fake_token_row):
    """Кэш пустой → load_one_ory_token загружает из БД → refresh вернул None → False."""
    chat_id = 12345
    assert chat_id not in client._tokens

    with patch(
        "db.queries.ory_tokens.load_one_ory_token",
        new_callable=AsyncMock,
        return_value=fake_token_row,
    ), patch(
        "db.queries.ory_tokens.refresh_ory_token_with_lock",
        new_callable=AsyncMock,
        return_value=None,
    ):
        ok = await client._refresh_single_token(chat_id)

    assert ok is False
    # Cache должен содержать defensive-reloaded токен (для диагностики),
    # но refresh не удался.
    assert chat_id in client._tokens
    assert client._tokens[chat_id]["access_token"] == "old_access_123"


# ─── GTW5: cache miss + no token in DB ───

@pytest.mark.asyncio
async def test_cache_miss_no_token_in_db(client):
    """Кэш пустой и в БД тоже нет → False, re-auth needed."""
    chat_id = 12345

    with patch(
        "db.queries.ory_tokens.load_one_ory_token",
        new_callable=AsyncMock,
        return_value=None,
    ):
        ok = await client._refresh_single_token(chat_id)

    assert ok is False
    assert chat_id not in client._tokens
