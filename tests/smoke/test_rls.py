"""
Smoke-тесты для db/rls.py — RLS context wrapper (WP-212 B4.24).

Проверяет:
1. Импорт без ошибок
2. SET LOCAL app.user_id выполняется при ory_id
3. SET LOCAL НЕ выполняется при ory_id = None/empty
4. ROLLBACK при ошибке в fn
5. Соединение возвращается в пул после ошибки
6. Результат fn возвращается корректно
7. Работает с None ory_id (платформенный запрос)
"""

import sys
import os
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path or sys.path.index(_PROJECT_ROOT) > 0:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


def _make_pool_mock(conn_mock):
    """Собрать мок asyncpg.Pool с acquire() как async context manager."""
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn_mock)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    pool_mock = MagicMock()
    pool_mock.acquire = MagicMock(return_value=acquire_cm)
    return pool_mock


def _make_conn_mock():
    """Собрать мок asyncpg.Connection с transaction() и execute()."""
    conn_mock = MagicMock()
    conn_mock.execute = AsyncMock()

    tx_cm = MagicMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    conn_mock.transaction = MagicMock(return_value=tx_cm)

    return conn_mock


def test_import():
    """Модуль импортируется без ошибок."""
    from db.rls import with_user_context
    assert callable(with_user_context)


@pytest.mark.asyncio
async def test_set_local_called_with_ory_id():
    """SET LOCAL app.user_id вызывается когда ory_id передан."""
    from db.rls import with_user_context

    conn_mock = _make_conn_mock()
    pool_mock = _make_pool_mock(conn_mock)

    result = await with_user_context(pool_mock, "test-ory-id-123", lambda conn: conn.execute("SELECT 1"))

    # Проверяем что SET LOCAL был вызван с правильным ory_id
    calls = conn_mock.execute.call_args_list
    set_local_call = calls[0]
    assert "SET LOCAL app.user_id" in set_local_call.args[0]
    assert set_local_call.args[1] == "test-ory-id-123"


@pytest.mark.asyncio
async def test_no_set_local_when_ory_id_none():
    """SET LOCAL НЕ вызывается при ory_id = None."""
    from db.rls import with_user_context

    conn_mock = _make_conn_mock()
    pool_mock = _make_pool_mock(conn_mock)

    await with_user_context(pool_mock, None, lambda conn: conn.execute("SELECT 1"))

    # execute вызван только один раз — для SELECT 1, не для SET LOCAL
    assert conn_mock.execute.call_count == 1
    assert "SET LOCAL" not in conn_mock.execute.call_args.args[0]


@pytest.mark.asyncio
async def test_no_set_local_when_ory_id_empty():
    """SET LOCAL НЕ вызывается при ory_id = '' (пустая строка)."""
    from db.rls import with_user_context

    conn_mock = _make_conn_mock()
    pool_mock = _make_pool_mock(conn_mock)

    await with_user_context(pool_mock, "", lambda conn: conn.execute("SELECT 1"))

    assert conn_mock.execute.call_count == 1
    assert "SET LOCAL" not in conn_mock.execute.call_args.args[0]


@pytest.mark.asyncio
async def test_fn_result_returned():
    """Результат fn возвращается корректно."""
    from db.rls import with_user_context

    conn_mock = _make_conn_mock()
    pool_mock = _make_pool_mock(conn_mock)

    async def fn(conn):
        return [{"id": 42, "name": "test"}]

    result = await with_user_context(pool_mock, "ory-id", fn)
    assert result == [{"id": 42, "name": "test"}]


@pytest.mark.asyncio
async def test_transaction_context_manager_used():
    """conn.transaction() вызывается как context manager."""
    from db.rls import with_user_context

    conn_mock = _make_conn_mock()
    pool_mock = _make_pool_mock(conn_mock)

    await with_user_context(pool_mock, "ory-id", lambda conn: conn.execute("SELECT 1"))

    conn_mock.transaction.assert_called_once()
    conn_mock.transaction.return_value.__aenter__.assert_called_once()
    conn_mock.transaction.return_value.__aexit__.assert_called_once()


@pytest.mark.asyncio
async def test_exception_propagates_and_connection_released():
    """Исключение из fn пробрасывается; acquire().__aexit__ всё равно вызывается."""
    from db.rls import with_user_context

    conn_mock = _make_conn_mock()
    # transaction.__aexit__ должен вернуть False (не подавлять исключение)
    conn_mock.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn_mock)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool_mock = MagicMock()
    pool_mock.acquire = MagicMock(return_value=acquire_cm)

    async def failing_fn(conn):
        raise ValueError("db error")

    with pytest.raises(ValueError, match="db error"):
        await with_user_context(pool_mock, "ory-id", failing_fn)

    # Соединение возвращено в пул (acquire().__aexit__ вызван)
    acquire_cm.__aexit__.assert_called_once()


@pytest.mark.asyncio
async def test_set_local_before_fn_call():
    """SET LOCAL вызывается ДО fn, не после."""
    from db.rls import with_user_context

    call_order = []

    conn_mock = MagicMock()

    async def mock_execute(query, *args):
        if "SET LOCAL" in query:
            call_order.append("set_local")
        else:
            call_order.append("user_query")

    conn_mock.execute = mock_execute

    tx_cm = MagicMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    conn_mock.transaction = MagicMock(return_value=tx_cm)

    pool_mock = _make_pool_mock(conn_mock)

    await with_user_context(pool_mock, "ory-id", lambda conn: conn.execute("SELECT 1"))

    assert call_order == ["set_local", "user_query"]
