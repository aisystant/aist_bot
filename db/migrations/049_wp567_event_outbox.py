"""
Migration 049: WP-567 Ф3(в) -- transactional outbox for critical bot events
(payment_received, subscription_granted) + idempotency on subscriptions.

Context (peer-session 2026-09-12-09-wp567-stars-retry-parnaya-zapis,
Claude+Kimi+Codex): the original ask ("надёжный повтор оплаты Telegram
Stars при недоступности базы баллов") assumed the Stars payment handler
wrote points directly and swallowed the error. Re-reading the actual code
(handlers/subscription_stars.py, helpers/dual_write.py) found a different
and narrower gap: the handler never writes points itself -- it emits
payment_received/subscription_granted via `asyncio.create_task(post_event(...))`,
a genuine fire-and-forget HTTP call with no durability of its own. Once an
event reaches `learning.domain_event`, the separate multi-domain-projection-
worker service already has its own cursor + DLQ + backoff retry, so that
part did not need a new queue. The two real gaps this migration closes:

  1. The event can be lost with zero trace if the bot process dies between
     `asyncio.create_task(...)` and the task actually running (Railway OOM,
     deploy, event-loop stall under load) -- narrow in CPU time, unbounded
     in wall-clock, and currently invisible.
  2. `save_subscription()` (db/queries/subscription.py) is called in a bare
     `try/except: logger.warning(...)`, no re-raise. `public.subscriptions`
     is NOT the "FSM-only, TTL~24h" marker its own comments claim -- it is
     the durable record used by `get_active_subscription`/`cancel_subscription`
     (core/access.py, states/common/settings.py). Losing this INSERT loses
     the user's subscription record after Stars were already charged.

Fix (Codex's transactional-outbox proposal, narrowed by Kimi to the two
critical event types only -- not a blanket policy for every event in the
bot): `on_successful_stars_sub` becomes one transaction that inserts the
subscription row AND both outbox rows, or none of them. A separate
dispatcher (this file's sibling change to core/scheduler.py) drains the
outbox with FOR UPDATE SKIP LOCKED, mirroring the existing
core/notification_service.py `drain()` pattern already used in this repo.

This migration:
  A. Creates `public.event_outbox` (id, external_id UNIQUE, event_type,
     account_id, payload jsonb, occurred_at, created_at, delivered_at,
     attempts, last_error). `external_id` here is the SAME value the
     handler already builds for the event-gateway envelope of each event
     type ("stars-sub-pay-{charge_id}" for payment_received,
     "sub-granted-{chat_id}-tg_stars-{charge_id}" for
     subscription_granted, db/queries/subscription.py
     `save_subscription_with_outbox`) -- not a new composite key. An
     earlier design draft assumed both events would share one bare
     `charge_id`, which a plain UNIQUE would have rejected on the second
     INSERT; re-reading the actual handler found the two event types
     already use distinct, non-colliding formats, and reusing them
     preserves the regex `^sub-granted-(\\d+)-` the projection-worker's
     rule 103 parses subscription_granted with -- inventing a different
     key here would have broken that parsing.
  B. Adds `UNIQUE (telegram_payment_charge_id)` to the existing
     `public.subscriptions` table, so a Telegram-redelivered update (after
     a non-2xx response caused by this migration's own new all-or-nothing
     transaction) cannot create a duplicate subscription row. Preflight
     checks for pre-existing duplicate charge_ids first and refuses with an
     actionable error rather than silently failing the ALTER -- there is no
     live read access to production from the agent preparing this file, so
     this cannot be verified in advance; the migration verifies it itself,
     fail-closed, before touching the table.

NOT applied to production by the agent that wrote this file -- prepared for
review only, same pattern as migrations 047/048 earlier today. This targets
the bot's own main database (config.DATABASE_URL / Railway `bot_data`), NOT
the `rewards` Neon database migrations 047/048 targeted -- different DSN,
different repo convention (see migration 046 for the same DATABASE_URL
pattern).

Manual run:
    python -m db.migrations.049_wp567_event_outbox
"""

import asyncio
import asyncpg


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        outbox_exists = await conn.fetchval(
            """SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'event_outbox'
            )"""
        )
        constraint_exists = await conn.fetchval(
            """SELECT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'subscriptions_charge_id_unique'
            )"""
        )
        if outbox_exists and constraint_exists:
            return False

        async with conn.transaction():
            if not outbox_exists:
                await conn.execute(
                    """CREATE TABLE public.event_outbox (
                        id BIGSERIAL PRIMARY KEY,
                        external_id TEXT NOT NULL UNIQUE,
                        event_type TEXT NOT NULL,
                        account_id TEXT,
                        payload JSONB NOT NULL,
                        occurred_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                        delivered_at TIMESTAMP,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT
                    )"""
                )
                await conn.execute(
                    """CREATE INDEX idx_event_outbox_pending
                       ON public.event_outbox(created_at)
                       WHERE delivered_at IS NULL"""
                )

            if not constraint_exists:
                dupes = await conn.fetch(
                    """SELECT telegram_payment_charge_id, count(*) AS n
                       FROM public.subscriptions
                       GROUP BY telegram_payment_charge_id
                       HAVING count(*) > 1"""
                )
                if dupes:
                    sample = ", ".join(row["telegram_payment_charge_id"] for row in dupes[:5])
                    raise RuntimeError(
                        f"049 preflight: {len(dupes)} duplicate telegram_payment_charge_id "
                        f"values in public.subscriptions (sample: {sample}) -- resolve "
                        "duplicates manually before this migration can add the UNIQUE "
                        "constraint. Not attempted automatically: deciding which duplicate "
                        "row is the real one is a business decision, not a safe default."
                    )
                await conn.execute(
                    """ALTER TABLE public.subscriptions
                       ADD CONSTRAINT subscriptions_charge_id_unique
                       UNIQUE (telegram_payment_charge_id)"""
                )
    return True


if __name__ == "__main__":
    from config import DATABASE_URL

    async def run():
        pool = await asyncpg.create_pool(DATABASE_URL)
        applied = await migrate_if_needed(pool)
        print(f"Migration 049: {'event_outbox created + subscriptions idempotency added' if applied else 'already applied'}")
        await pool.close()

    asyncio.run(run())
