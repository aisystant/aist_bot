"""
Кеш контента — экономия Claude API на повторной генерации.

Cache key format:
- practice:{topic_id}:{lang}         — практическое задание
- question:{topic_id}:{bloom}:{lang} — вопрос по теме
"""

from datetime import datetime, timedelta
from typing import Optional

from config import get_logger, MOSCOW_TZ
from db.connection import get_pool

logger = get_logger(__name__)

# TTL по умолчанию: 7 дней (контент программы меняется редко)
DEFAULT_TTL_DAYS = 7


async def cache_get(cache_key: str) -> Optional[str]:
    """Получить контент из кеша. Возвращает None если нет или истёк."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT content FROM content_cache
               WHERE cache_key = $1 AND expires_at > NOW()''',
            cache_key
        )
    if row:
        logger.info(f"[Cache] HIT: {cache_key}")
        return row['content']
    return None


async def cache_set(cache_key: str, content_type: str, content: str, ttl_days: int = DEFAULT_TTL_DAYS):
    """Сохранить контент в кеш."""
    pool = await get_pool()
    expires = datetime.now(MOSCOW_TZ) + timedelta(days=ttl_days)
    async with pool.acquire() as conn:
        await conn.execute(
            '''INSERT INTO content_cache (cache_key, content_type, content, expires_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (cache_key)
               DO UPDATE SET content = $3, expires_at = $4, created_at = NOW()''',
            cache_key, content_type, content, expires
        )
    logger.info(f"[Cache] SET: {cache_key} (expires {expires.date()})")


async def cache_cleanup():
    """Удалить истекшие записи. Вызывается из scheduler раз в сутки."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            'DELETE FROM content_cache WHERE expires_at < NOW()'
        )
    logger.info(f"[Cache] Cleanup: {result}")
