"""
Запросы для пре-генерированного контента Марафона (marathon_content).
"""

import json

from db import get_pool
from db.queries.users import moscow_today
from config import get_logger

logger = get_logger(__name__)


async def save_marathon_content(
    chat_id: int,
    topic_index: int,
    lesson_content: str = None,
    question_content: str = None,
    practice_content: dict = None,
    bloom_level: int = None,
):
    """Сохранить пре-генерированный контент для марафона.

    Использует UPSERT: если контент для этого пользователя+темы уже есть — обновляет.
    """
    practice_json = json.dumps(practice_content) if practice_content else None

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''INSERT INTO marathon_content
               (chat_id, topic_index, lesson_content, question_content, practice_content, bloom_level)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (chat_id, topic_index) DO UPDATE SET
                   lesson_content = COALESCE($3, marathon_content.lesson_content),
                   question_content = COALESCE($4, marathon_content.question_content),
                   practice_content = COALESCE($5, marathon_content.practice_content),
                   bloom_level = COALESCE($6, marathon_content.bloom_level),
                   status = 'pending',
                   created_at = NOW(),
                   delivered_at = NULL
            ''',
            chat_id, topic_index, lesson_content, question_content, practice_json, bloom_level,
        )
    logger.info(f"[Marathon] Saved pre-generated content for {chat_id}, topic {topic_index}")


async def get_marathon_content(chat_id: int, topic_index: int) -> dict | None:
    """Получить пре-генерированный контент.

    Returns:
        dict с ключами lesson_content, question_content, practice_content (parsed JSON),
        bloom_level, status. Или None если не найден.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT * FROM marathon_content
               WHERE chat_id = $1 AND topic_index = $2''',
            chat_id, topic_index,
        )

    if not row:
        return None

    result = dict(row)
    # Parse practice_content JSON
    if result.get('practice_content'):
        try:
            result['practice_content'] = json.loads(result['practice_content'])
        except (json.JSONDecodeError, TypeError):
            result['practice_content'] = None

    return result


async def mark_content_delivered(chat_id: int, topic_index: int):
    """Отметить контент как доставленный."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''UPDATE marathon_content
               SET status = 'delivered', delivered_at = NOW()
               WHERE chat_id = $1 AND topic_index = $2''',
            chat_id, topic_index,
        )


async def cleanup_expired_content():
    """Удалить невостребованный пре-генерированный контент.

    Вызывается в полночь (00:00 MSK).
    Marathon: удаляет pending, созданные до сегодня.
    Feed: удаляет orphaned pending (UPSERT защищает от дублей внутри одной недели,
          но не от pending от завершённых/неактивных недель).
    """
    today = moscow_today()
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            '''DELETE FROM marathon_content
               WHERE status = 'pending' AND created_at::date < $1''',
            today,
        )
        logger.info(f"[Marathon] Cleanup expired content: {result}")

        # Feed: expire (не delete) — сохраняем для аналитики
        feed_result = await conn.execute(
            '''UPDATE feed_sessions SET status = 'expired'
               WHERE status IN ('pending', 'active') AND session_date < $1''',
            today,
        )
        if feed_result and feed_result != 'UPDATE 0':
            logger.info(f"[Feed] Midnight auto-expire: {feed_result}")
