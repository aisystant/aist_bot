"""
RLS account context wrapper — WP-212 B4.24, rewritten in WP-544 F16.

Row-level policies on the Neon indicators tables (digital_twins,
calculated_profile) read the caller's account through app.require_account_id(),
which takes app.account_id first and raises when no context setting is present.
The bot used to pass those policies only because the owner role bypassed RLS;
its own roles do not.

Usage:
    from db.rls import with_user_context

    rows = await with_user_context(pool, str(account_id), lambda conn:
        conn.fetch("SELECT ... FROM public.calculated_profile WHERE account_id = $1::uuid",
                   str(account_id))
    )

Guarantees:
  - set_config(..., is_local => true) lasts until the end of the transaction,
    so the pooled connection goes back without the context.
  - The context and fn share one connection and one explicit transaction.
"""

from __future__ import annotations

from typing import Awaitable, Callable, TypeVar

import asyncpg

T = TypeVar("T")

# SET LOCAL takes no bind parameters; set_config does.
_SET_ACCOUNT_CONTEXT = "SELECT set_config('app.account_id', $1, true)"


async def with_user_context(
    pool: asyncpg.Pool,
    account_id: str | None,
    fn: Callable[[asyncpg.Connection], Awaitable[T]],
) -> T:
    """Run fn in a transaction whose app.account_id is account_id.

    Args:
        pool: asyncpg pool of the database whose policies read app.account_id.
        account_id: the value the policy compares rows with (Ory account id or
            twin user id, as a string). None/empty sets no context, as before
            the rewrite: policies built on app.require_account_id() then fail
            inside the database instead of reading someone else's rows.
        fn: async function receiving the connection holding the context.

    Returns:
        The result of fn.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            if account_id:
                await conn.execute(_SET_ACCOUNT_CONTEXT, account_id)
            return await fn(conn)
