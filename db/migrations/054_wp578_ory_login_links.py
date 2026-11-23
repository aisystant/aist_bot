"""
Миграция 054: ory_login_links — отметка «этот Telegram привязан к аккаунту входом через Ory» (РП-578 Ф29 п.8).

Нужна, чтобы бот сохранял самооценку /diagnose за участника (узкий пропуск, решение
DS-my-strategy inbox/WP-578/DRR-f29-on-behalf-self-assessment.md) только в аккаунт, к которому
человек сам вошёл через Ory. public.users.ory_id для этого не годится: его пишут и старая
привязка Aisystant, и скрипты администратора, а явное отключение его не снимает.

Писатели: возврат из Ory (oauth_server.ory_callback_handler) ставит отметку; явное отключение
человеком (gateway_mcp.disconnect) её удаляет. Отзыв пропуска со стороны Ory (invalid_grant)
отметку не трогает — человек не отказывался.

Живёт в базе секретов рядом с public.ory_tokens. При создании переносит привязки тех, у кого
пропуск входа есть сейчас (public.ory_tokens.ory_id). Номера чатов наружу не выводятся.

Колонки:
  chat_id     — Telegram chat id (ключ)
  account_id  — аккаунт Ory, в который вошёл человек
  linked_at   — когда (UTC, без часового пояса — как остальные TIMESTAMP-колонки бота)

Запуск вручную:
    python -m db.migrations.054_wp578_ory_login_links
"""

import asyncio

import asyncpg


async def migrate_if_needed(pool: asyncpg.Pool) -> bool:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'ory_login_links'
            )
            """
        )
        if exists:
            return False
        async with conn.transaction():
            await conn.execute(
                """
                CREATE TABLE public.ory_login_links (
                    chat_id BIGINT PRIMARY KEY,
                    account_id UUID NOT NULL,
                    linked_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
                )
                """
            )
            await conn.execute(
                """
                INSERT INTO public.ory_login_links (chat_id, account_id, linked_at)
                SELECT chat_id, ory_id::uuid, COALESCE(updated_at, NOW() AT TIME ZONE 'utc')
                FROM public.ory_tokens
                WHERE ory_id IS NOT NULL
                ON CONFLICT (chat_id) DO NOTHING
                """
            )
        return True


async def main() -> None:
    from db.connection import get_secrets_pool

    created = await migrate_if_needed(await get_secrets_pool())
    print("ory_login_links created" if created else "ory_login_links already exists")


if __name__ == "__main__":
    asyncio.run(main())
