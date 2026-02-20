"""
DB-запросы для Discourse-интеграции (WP-53).
"""

from datetime import datetime, timezone
from db.connection import get_pool
from config import get_logger

logger = get_logger(__name__)


# ── Аккаунты ───────────────────────────────────────────────

async def get_discourse_account(chat_id: int) -> dict | None:
    """Получить привязанный аккаунт Discourse."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM discourse_accounts WHERE chat_id = $1", chat_id
    )
    return dict(row) if row else None


async def link_discourse_account(
    chat_id: int,
    discourse_username: str,
    blog_category_id: int | None = None,
    blog_category_slug: str | None = None,
) -> None:
    """Привязать аккаунт Discourse к пользователю бота."""
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO discourse_accounts (chat_id, discourse_username, blog_category_id, blog_category_slug)
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
    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM discourse_accounts WHERE chat_id = $1", chat_id
    )
    return result != "DELETE 0"


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
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO published_posts (chat_id, discourse_topic_id, discourse_post_id, title, category_id, source_file)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (discourse_topic_id) DO NOTHING
        """,
        chat_id, discourse_topic_id, discourse_post_id, title, category_id, source_file,
    )


async def get_published_posts(chat_id: int) -> list[dict]:
    """Список опубликованных постов пользователя."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM published_posts
        WHERE chat_id = $1
        ORDER BY published_at DESC
        """,
        chat_id,
    )
    return [dict(r) for r in rows]


async def is_title_published(chat_id: int, title: str) -> bool:
    """Проверить, опубликован ли пост с таким заголовком (дедупликация)."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM published_posts WHERE chat_id = $1 AND lower(title) = lower($2)",
        chat_id, title,
    )
    return row is not None


async def update_post_comments_count(discourse_topic_id: int, posts_count: int) -> None:
    """Обновить количество постов (комментариев) в топике."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE published_posts
        SET posts_count = $2, last_checked_at = NOW()
        WHERE discourse_topic_id = $1
        """,
        discourse_topic_id, posts_count,
    )


async def get_posts_for_comment_check() -> list[dict]:
    """Получить посты для проверки комментариев (polling)."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT pp.*, da.discourse_username
        FROM published_posts pp
        JOIN discourse_accounts da ON pp.chat_id = da.chat_id
        ORDER BY pp.published_at DESC
        LIMIT 100
        """
    )
    return [dict(r) for r in rows]


# ── Запланированные публикации ─────────────────────────────

async def schedule_publication(
    chat_id: int,
    title: str,
    raw: str,
    category_id: int,
    schedule_time: datetime,
    tags: str = "[]",
) -> int:
    """Запланировать публикацию."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO scheduled_publications (chat_id, title, raw, category_id, schedule_time, tags)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        chat_id, title, raw, category_id, schedule_time, tags,
    )
    return row["id"]


async def get_pending_publications() -> list[dict]:
    """Получить публикации, готовые к отправке."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT sp.*, da.discourse_username
        FROM scheduled_publications sp
        JOIN discourse_accounts da ON sp.chat_id = da.chat_id
        WHERE sp.status = 'pending' AND sp.schedule_time <= NOW()
        ORDER BY sp.schedule_time
        """
    )
    return [dict(r) for r in rows]


async def mark_publication_done(pub_id: int, discourse_topic_id: int) -> None:
    """Пометить публикацию как выполненную."""
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE scheduled_publications
        SET status = 'published', discourse_topic_id = $2
        WHERE id = $1
        """,
        pub_id, discourse_topic_id,
    )


async def mark_publication_failed(pub_id: int) -> None:
    """Пометить публикацию как неудачную."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE scheduled_publications SET status = 'failed' WHERE id = $1",
        pub_id,
    )
