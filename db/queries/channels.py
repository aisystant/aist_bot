"""
Запросы для мониторинга каналов (SC.118).

WP-253 lift-and-shift (8 мая 2026): таблицы переехали в Neon publication БД.
- channel_monitors → publication.channel_monitor (publication pool)
- channel_mentions_log → publication.channel_mention_log (publication pool)

NOTE: JOIN с public.users / development.user_state остался в legacy bot_data
(get_pool). После миграции users → persona — главный агент перепишет JOIN.
"""

from typing import Optional

from config import get_logger
from db.connection import get_pool, get_publication_pool

logger = get_logger(__name__)


async def get_monitors_for_channel(channel_id: int) -> list[dict]:
    """Получить все активные мониторы для канала.

    Cross-DB enrichment: основная таблица в publication БД, поля пользователя
    (name, tg_username, language) в bot_data.public.users — вытаскиваем
    отдельным запросом и мёрджим в Python.
    """
    pub_pool = await get_publication_pool()
    rows = await pub_pool.fetch(
        '''
        SELECT * FROM channel_monitor
        WHERE channel_id = $1 AND active = TRUE
        ''',
        channel_id,
    )
    monitors = [dict(r) for r in rows]
    if not monitors:
        return []

    # Enrich user fields из legacy public.users
    user_ids = list({m["user_id"] for m in monitors})
    legacy_pool = await get_pool()
    async with legacy_pool.acquire() as conn:
        users = await conn.fetch(
            '''
            SELECT id, name, tg_username, language
            FROM public.users
            WHERE id = ANY($1::uuid[])
            ''',
            user_ids,
        )
    user_by_id = {str(u["id"]): u for u in users}
    for m in monitors:
        u = user_by_id.get(str(m["user_id"]))
        if u:
            m["name"] = u["name"]
            m["tg_username"] = u["tg_username"]
            m["language"] = u["language"]
        else:
            m.setdefault("name", None)
            m.setdefault("tg_username", None)
            m.setdefault("language", None)
    return monitors


async def get_user_monitors(chat_id: int) -> list[dict]:
    """Получить все мониторы пользователя."""
    pool = await get_publication_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT * FROM channel_monitor
            WHERE chat_id = $1
            ORDER BY created_at DESC
        ''', chat_id)
        return [dict(r) for r in rows]


async def upsert_monitor(
    channel_id: int,
    channel_title: str,
    user_id: str,
    chat_id: int,
    is_admin: bool = False,
) -> dict:
    """Создать или обновить монитор канала."""
    pool = await get_publication_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            INSERT INTO channel_monitor (channel_id, channel_title, user_id, chat_id, is_admin)
            VALUES ($1, $2, $3::uuid, $4, $5)
            ON CONFLICT (channel_id, chat_id)
            DO UPDATE SET
                channel_title = EXCLUDED.channel_title,
                is_admin = EXCLUDED.is_admin,
                active = TRUE,
                updated_at = NOW() AT TIME ZONE 'utc'
            RETURNING *
        ''', channel_id, channel_title, user_id, chat_id, is_admin)
        return dict(row)


async def deactivate_monitor(channel_id: int, chat_id: int) -> bool:
    """Деактивировать монитор."""
    pool = await get_publication_pool()
    async with pool.acquire() as conn:
        result = await conn.execute('''
            UPDATE channel_monitor SET active = FALSE, updated_at = NOW() AT TIME ZONE 'utc'
            WHERE channel_id = $1 AND chat_id = $2
        ''', channel_id, chat_id)
        return result != 'UPDATE 0'


async def is_mention_logged(channel_id: int, message_id: int, mentioned_chat_id: int) -> bool:
    """Проверить, было ли уведомление уже отправлено (дедупликация)."""
    pool = await get_publication_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval('''
            SELECT EXISTS(
                SELECT 1 FROM channel_mention_log
                WHERE channel_id = $1 AND message_id = $2 AND mentioned_chat_id = $3
            )
        ''', channel_id, message_id, mentioned_chat_id)


async def log_mention(
    channel_id: int,
    message_id: int,
    mentioned_chat_id: int,
    mention_type: str,
    draft_sent: bool = False,
) -> None:
    """Записать факт уведомления (log-before-send)."""
    pool = await get_publication_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO channel_mention_log (channel_id, message_id, mentioned_chat_id, mention_type, draft_sent)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (channel_id, message_id, mentioned_chat_id) DO NOTHING
        ''', channel_id, message_id, mentioned_chat_id, mention_type, draft_sent)


async def find_user_by_username(username: str) -> Optional[dict]:
    """Найти пользователя бота по TG username.

    JOIN остаётся в legacy bot_data (public.users + development.user_state) —
    эти таблицы мигрирует главный агент (TODO: миграция users → persona).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT u.id AS user_id, u.telegram_id AS chat_id, u.name, u.tg_username, u.language,
                   s.onboarding_completed
            FROM public.users u
            LEFT JOIN development.user_state s ON s.user_id = u.id
            WHERE LOWER(u.tg_username) = LOWER($1)
        ''', username)
        return dict(row) if row else None


async def find_user_by_name(name: str) -> Optional[dict]:
    """Найти пользователя бота по имени (exact match, case-insensitive).

    JOIN остаётся в legacy bot_data (public.users + development.user_state) —
    эти таблицы мигрирует главный агент (TODO: миграция users → persona).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT u.id AS user_id, u.telegram_id AS chat_id, u.name, u.tg_username, u.language,
                   s.onboarding_completed
            FROM public.users u
            LEFT JOIN development.user_state s ON s.user_id = u.id
            WHERE LOWER(u.name) = LOWER($1)
        ''', name)
        return dict(row) if row else None
