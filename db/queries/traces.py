"""
Запросы для таблицы request_traces (трейсинг запросов).
"""

from typing import List, Optional
from db.connection import acquire
from config import get_logger

logger = get_logger(__name__)


async def cleanup_old_traces(days: int = 7) -> int:
    """Удалить traces старше N дней. Возвращает количество удалённых."""
    async with await acquire() as conn:
        result = await conn.execute(
            "DELETE FROM request_traces WHERE created_at < NOW() - INTERVAL '1 day' * $1",
            days,
        )
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info(f"[Traces] Cleaned up {count} traces older than {days} days")
        return count
