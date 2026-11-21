"""Счётчики выпуска А агента-собеседника (РП-262 Ф10). Только числа: ни текстов, ни пропусков, ни чатов."""

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


async def bump(kind: str, elapsed_ms: int = 0) -> None:
    """Add one event of `kind` to today's row. A failure is logged and swallowed: a counter must never
    break the answer to a person."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO interlocutor_stats (day, kind, n, total_ms)
                VALUES ((NOW() AT TIME ZONE 'UTC')::date, $1, 1, $2)
                ON CONFLICT (day, kind) DO UPDATE
                SET n = interlocutor_stats.n + 1, total_ms = interlocutor_stats.total_ms + EXCLUDED.total_ms
                """,
                kind,
                max(0, int(elapsed_ms)),
            )
    except Exception as exc:
        logger.warning("[InterlocutorStats] bump failed: %s", type(exc).__name__)
