from __future__ import annotations

"""
DB-запросы для Discourse-интеграции (WP-53).

WP-253 lift-and-shift (8 мая 2026): таблицы переехали в Neon BC БД.
- discourse_accounts → community.club_account (community pool)
- published_posts → publication.published_post (publication pool)
- scheduled_publications → publication.scheduled_post (publication pool)
"""

from datetime import datetime, timedelta, timezone
from db.connection import get_community_pool, get_publication_pool
from config import get_logger

logger = get_logger(__name__)


# ── Аккаунты ───────────────────────────────────────────────

async def get_discourse_account(chat_id: int) -> dict | None:
    """Получить привязанный аккаунт Discourse."""
    pool = await get_community_pool()
    row = await pool.fetchrow(
        "SELECT * FROM club_account WHERE chat_id = $1", chat_id
    )
    return dict(row) if row else None


async def link_discourse_account(
    chat_id: int,
    discourse_username: str,
    blog_category_id: int | None = None,
    blog_category_slug: str | None = None,
) -> None:
    """Привязать аккаунт Discourse к пользователю бота."""
    pool = await get_community_pool()
    await pool.execute(
        """
        INSERT INTO club_account (chat_id, discourse_username, blog_category_id, blog_category_slug)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (chat_id) DO UPDATE SET
            discourse_username = $2,
            blog_category_id = $3,
            blog_category_slug = $4,
            connected_at = NOW()
        """,
        chat_id, discourse_username, blog_category_id, blog_category_slug,
    )


async def unlink_discourse_account(chat_id: int) -> bool:
    """Отвязать аккаунт Discourse."""
    pool = await get_community_pool()
    result = await pool.execute(
        "DELETE FROM club_account WHERE chat_id = $1", chat_id
    )
    return result != "DELETE 0"


async def get_chat_id_by_discourse_username(username: str) -> int | None:
    """Найти chat_id по discourse_username (для входящих webhook-событий)."""
    pool = await get_community_pool()
    row = await pool.fetchrow(
        "SELECT chat_id FROM club_account WHERE discourse_username = $1", username
    )
    return row["chat_id"] if row else None


# ── Публикации ─────────────────────────────────────────────

async def save_published_post(
    chat_id: int,
    discourse_topic_id: int,
    discourse_post_id: int | None,
    title: str,
    category_id: int,
    source_file: str | None = None,
) -> None:
    """Сохранить информацию об опубликованном посте."""
    pool = await get_publication_pool()
    await pool.execute(
        """
        INSERT INTO published_post (chat_id, discourse_topic_id, discourse_post_id, title, category_id, source_file)
        SELECT $1, $2, $3, $4, $5, $6
        WHERE NOT EXISTS (SELECT 1 FROM published_post WHERE discourse_topic_id = $2)
        """,
        chat_id, discourse_topic_id, discourse_post_id, title, category_id, source_file,
    )


async def get_published_posts(chat_id: int) -> list[dict]:
    """Список опубликованных постов пользователя."""
    pool = await get_publication_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM published_post
        WHERE chat_id = $1
        ORDER BY published_at DESC
        """,
        chat_id,
    )
    return [dict(r) for r in rows]


async def is_title_published(chat_id: int, title: str) -> bool:
    """Проверить, опубликован ли пост с таким заголовком (дедупликация)."""
    pool = await get_publication_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM published_post WHERE chat_id = $1 AND lower(title) = lower($2)",
        chat_id, title,
    )
    return row is not None


async def update_post_comments_count(discourse_topic_id: int, posts_count: int) -> None:
    """Обновить количество постов (комментариев) в топике."""
    pool = await get_publication_pool()
    await pool.execute(
        """
        UPDATE published_post
        SET posts_count = $2, last_checked_at = NOW()
        WHERE discourse_topic_id = $1
        """,
        discourse_topic_id, posts_count,
    )


async def get_posts_for_comment_check() -> list[dict]:
    """Получить посты для проверки комментариев (polling).

    Исключает посты с 3+ неудачными проверками (удалённые/недоступные топики).

    NOTE (WP-253 lift-and-shift): JOIN с club_account через cross-DB не работает
    из одного pool. Делаем отдельный запрос club_account и enrich в Python.
    """
    pub_pool = await get_publication_pool()
    rows = await pub_pool.fetch(
        """
        SELECT pp.*
        FROM published_post pp
        WHERE COALESCE(pp.comment_check_failures, 0) < 3
        ORDER BY pp.published_at DESC
        LIMIT 100
        """
    )
    posts = [dict(r) for r in rows]
    if not posts:
        return []

    # Enrich discourse_username из community.club_account
    chat_ids = list({p["chat_id"] for p in posts})
    com_pool = await get_community_pool()
    accounts = await com_pool.fetch(
        "SELECT chat_id, discourse_username FROM club_account WHERE chat_id = ANY($1::bigint[])",
        chat_ids,
    )
    username_by_chat = {a["chat_id"]: a["discourse_username"] for a in accounts}
    for p in posts:
        p["discourse_username"] = username_by_chat.get(p["chat_id"])
    return posts


async def increment_comment_check_failures(discourse_topic_id: int) -> None:
    """Инкремент счётчика неудачных проверок комментариев."""
    pool = await get_publication_pool()
    await pool.execute(
        """
        UPDATE published_post
        SET comment_check_failures = COALESCE(comment_check_failures, 0) + 1
        WHERE discourse_topic_id = $1
        """,
        discourse_topic_id,
    )


# ── Запланированные публикации ─────────────────────────────

async def schedule_publication(
    chat_id: int,
    title: str,
    raw: str,
    category_id: int,
    schedule_time: datetime,
    tags: str = "[]",
    source_file: str | None = None,
) -> int:
    """Запланировать публикацию."""
    # Safeguard: scheduled_post.schedule_time — TIMESTAMP (naive).
    # asyncpg не может записать aware datetime в naive колонку → DataError.
    # См. CLAUDE.md § 10.5, error_logs #449.
    if schedule_time.tzinfo is not None:
        schedule_time = schedule_time.replace(tzinfo=None)
    pool = await get_publication_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO scheduled_post (chat_id, title, raw, category_id, schedule_time, tags, source_file)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        chat_id, title, raw, category_id, schedule_time, tags, source_file,
    )
    return row["id"]


async def get_pending_publications() -> list[dict]:
    """Получить публикации, готовые к отправке.

    NOTE (WP-253 lift-and-shift): JOIN с club_account через cross-DB не работает
    из одного pool. Делаем отдельный запрос и enrich в Python.
    """
    pub_pool = await get_publication_pool()
    rows = await pub_pool.fetch(
        """
        SELECT sp.*
        FROM scheduled_post sp
        WHERE sp.status = 'pending' AND sp.schedule_time <= NOW()
        ORDER BY sp.schedule_time
        """
    )
    pubs = [dict(r) for r in rows]
    if not pubs:
        return []

    chat_ids = list({p["chat_id"] for p in pubs})
    com_pool = await get_community_pool()
    accounts = await com_pool.fetch(
        "SELECT chat_id, discourse_username FROM club_account WHERE chat_id = ANY($1::bigint[])",
        chat_ids,
    )
    username_by_chat = {a["chat_id"]: a["discourse_username"] for a in accounts}
    for p in pubs:
        p["discourse_username"] = username_by_chat.get(p["chat_id"])
    return pubs


async def mark_publication_done(pub_id: int, discourse_topic_id: int) -> None:
    """Пометить публикацию как выполненную."""
    pool = await get_publication_pool()
    await pool.execute(
        """
        UPDATE scheduled_post
        SET status = 'published', discourse_topic_id = $2
        WHERE id = $1
        """,
        pub_id, discourse_topic_id,
    )


async def mark_publication_failed(pub_id: int) -> None:
    """Пометить публикацию как неудачную."""
    pool = await get_publication_pool()
    await pool.execute(
        "UPDATE scheduled_post SET status = 'failed' WHERE id = $1",
        pub_id,
    )


# ── Smart Publisher (R21, WP-53 Phase 3) ──────────────────────


async def get_all_published_source_files(chat_id: int) -> set[str]:
    """Множество source_file опубликованных постов (для reconciliation)."""
    pool = await get_publication_pool()
    rows = await pool.fetch(
        "SELECT source_file FROM published_post WHERE chat_id = $1 AND source_file IS NOT NULL",
        chat_id,
    )
    return {r["source_file"] for r in rows}


async def get_all_published_titles_lower(chat_id: int) -> set[str]:
    """Множество title (lowercase) опубликованных постов (для reconciliation по title)."""
    pool = await get_publication_pool()
    rows = await pool.fetch(
        "SELECT lower(title) as t FROM published_post WHERE chat_id = $1",
        chat_id,
    )
    return {r["t"] for r in rows}


async def get_all_scheduled_source_files(chat_id: int) -> set[str]:
    """Множество source_file запланированных постов (для reconciliation)."""
    pool = await get_publication_pool()
    rows = await pool.fetch(
        """
        SELECT sp.source_file FROM scheduled_post sp
        WHERE sp.chat_id = $1 AND sp.status = 'pending' AND sp.source_file IS NOT NULL
        """,
        chat_id,
    )
    return {r["source_file"] for r in rows}


async def get_all_scheduled_titles_lower(chat_id: int) -> set[str]:
    """Множество title (lowercase) запланированных постов (для reconciliation по title)."""
    pool = await get_publication_pool()
    rows = await pool.fetch(
        "SELECT lower(title) as t FROM scheduled_post WHERE chat_id = $1 AND status = 'pending'",
        chat_id,
    )
    return {r["t"] for r in rows}


async def get_scheduled_count(chat_id: int) -> int:
    """Количество постов в очереди (pending)."""
    pool = await get_publication_pool()
    row = await pool.fetchrow(
        "SELECT COUNT(*) as cnt FROM scheduled_post WHERE chat_id = $1 AND status = 'pending'",
        chat_id,
    )
    return row["cnt"] if row else 0


async def get_scheduled_dates(chat_id: int) -> set:
    """Множество дат (date objects), на которые уже есть pending публикации."""
    pool = await get_publication_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT schedule_time::date as d
        FROM scheduled_post
        WHERE chat_id = $1 AND status = 'pending'
        """,
        chat_id,
    )
    return {r["d"] for r in rows}


async def get_upcoming_schedule(chat_id: int, limit: int = 10) -> list[dict]:
    """Ближайшие запланированные публикации для отображения."""
    pool = await get_publication_pool()
    rows = await pool.fetch(
        """
        SELECT id, title, schedule_time, status, source_file
        FROM scheduled_post
        WHERE chat_id = $1 AND status = 'pending'
        ORDER BY schedule_time
        LIMIT $2
        """,
        chat_id, limit,
    )
    return [dict(r) for r in rows]


async def get_all_discourse_accounts() -> list[dict]:
    """Все привязанные аккаунты Discourse (для scheduler scan)."""
    pool = await get_community_pool()
    rows = await pool.fetch("SELECT * FROM club_account")
    return [dict(r) for r in rows]


async def get_scheduled_publication(pub_id: int) -> dict | None:
    """Получить полные данные запланированной публикации по ID.

    NOTE (WP-253 lift-and-shift): JOIN с club_account через cross-DB не работает
    из одного pool. Делаем отдельный запрос и enrich в Python.
    """
    pub_pool = await get_publication_pool()
    row = await pub_pool.fetchrow(
        """
        SELECT sp.*
        FROM scheduled_post sp
        WHERE sp.id = $1 AND sp.status = 'pending'
        """,
        pub_id,
    )
    if not row:
        return None
    pub = dict(row)
    com_pool = await get_community_pool()
    acc = await com_pool.fetchrow(
        "SELECT discourse_username FROM club_account WHERE chat_id = $1",
        pub["chat_id"],
    )
    pub["discourse_username"] = acc["discourse_username"] if acc else None
    return pub


async def cancel_scheduled_publication(pub_id: int) -> None:
    """Отменить запланированную публикацию."""
    pool = await get_publication_pool()
    await pool.execute(
        "DELETE FROM scheduled_post WHERE id = $1 AND status = 'pending'",
        pub_id,
    )


async def reschedule_publication(pub_id: int, new_time: datetime) -> None:
    """Перенести публикацию на новое время."""
    pool = await get_publication_pool()
    await pool.execute(
        "UPDATE scheduled_post SET schedule_time = $2 WHERE id = $1 AND status = 'pending'",
        pub_id, new_time,
    )


async def reschedule_all_pending(
    chat_id: int, pub_days: list[int], hour: int, minute: int,
) -> tuple[list[tuple[str, datetime]], int]:
    """Перераспределить все pending посты по новому расписанию.

    Удаляет дубли (по source_file), перераспределяет по слотам.
    Returns (list of (title, new_schedule_time), removed_duplicates_count).
    """
    from datetime import timedelta
    from db.queries.users import moscow_now

    pool = await get_publication_pool()

    rows = await pool.fetch(
        """
        SELECT id, title, source_file, schedule_time
        FROM scheduled_post
        WHERE chat_id = $1 AND status = 'pending'
        ORDER BY schedule_time
        """,
        chat_id,
    )
    if not rows:
        return [], 0

    # Dedup by source_file (keep first occurrence)
    seen_files: set[str] = set()
    unique_posts = []
    duplicate_ids = []
    for r in rows:
        sf = r["source_file"]
        if sf and sf in seen_files:
            duplicate_ids.append(r["id"])
        else:
            if sf:
                seen_files.add(sf)
            unique_posts.append(r)

    # Delete duplicates
    if duplicate_ids:
        await pool.execute(
            "DELETE FROM scheduled_post WHERE id = ANY($1::int[])",
            duplicate_ids,
        )

    # Generate new slots starting from tomorrow
    now_msk = moscow_now()
    slots: list[datetime] = []
    check_date = now_msk.date() + timedelta(days=1)

    from config.settings import PUBLISHER_INTERVAL

    for _ in range(60):
        if check_date.weekday() in pub_days:
            slot_time = datetime.combine(
                check_date,
                datetime.min.time().replace(hour=hour, minute=minute),
            ) - timedelta(hours=3)  # MSK→UTC
            slots.append(slot_time)
            if len(slots) >= len(unique_posts):
                break
            # Пропуск по интервалу (1 раз в N дней)
            check_date += timedelta(days=PUBLISHER_INTERVAL)
            continue
        check_date += timedelta(days=1)

    # Update schedule_time for each post
    result = []
    for post, slot in zip(unique_posts, slots):
        await pool.execute(
            "UPDATE scheduled_post SET schedule_time = $2 WHERE id = $1",
            post["id"], slot,
        )
        result.append((post["title"], slot))

    return result, len(duplicate_ids)
