"""
RLS context wrapper — WP-212 B4.24

Устанавливает SET LOCAL app.user_id внутри транзакции.
RLS-политики (migration 006, aist_bot migrations) читают current_user_id() = current_setting('app.user_id', true).

Использование:
    from db.rls import with_user_context

    result = await with_user_context(pool, ory_id, async def(conn):
        return await conn.fetch("SELECT * FROM digital_twins WHERE ...")
    )

    # Или через get_pool():
    pool = await get_pool()
    rows = await with_user_context(pool, user.ory_id, lambda conn:
        conn.fetch("SELECT * FROM public.users WHERE id = $1", user.id)
    )

Гарантии:
  - SET LOCAL действует только до конца транзакции (COMMIT/ROLLBACK сбрасывает)
  - Соединение возвращается в пул с чистым состоянием
  - Если ory_id = None/empty — SET LOCAL не вызывается → RLS блокирует личные строки (fail safe)
"""

from __future__ import annotations

from typing import Awaitable, Callable, TypeVar

import asyncpg

T = TypeVar("T")


async def with_user_context(
    pool: asyncpg.Pool,
    ory_id: str | None,
    fn: Callable[[asyncpg.Connection], Awaitable[T]],
) -> T:
    """Выполнить fn внутри транзакции с установленным app.user_id.

    Args:
        pool: asyncpg Pool (из get_pool())
        ory_id: Ory user UUID. None/empty → SET LOCAL не вызывается (платформенный запрос)
        fn: async-функция, получающая asyncpg.Connection

    Returns:
        Результат fn
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            if ory_id:
                await conn.execute("SET LOCAL app.user_id = $1", ory_id)
            return await fn(conn)
