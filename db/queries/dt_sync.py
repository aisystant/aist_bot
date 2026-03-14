"""
Синхронизация engagement данных из Neon → digital_twins JSONB (WP-85, Phase 4).

Бот и DT MCP делят одну Neon БД. Sync job пишет напрямую в digital_twins,
минуя HTTP. DT MCP читает при запросе пользователя.

Частота: ежедневно (scheduler cron).
"""

import json
import logging
from datetime import datetime, timezone

from db.connection import get_pool

logger = logging.getLogger(__name__)


async def sync_engagement_to_dt() -> dict:
    """Синхронизировать engagement данные всех пользователей в digital_twins.

    Читает development.engagement view, маппит на 4 группы метамодели (2_collected),
    пишет в digital_twins.data JSONB через INSERT ON CONFLICT.

    Returns:
        {"synced": N, "skipped": N, "errors": N, "first_error": str|None}
    """
    pool = await get_pool()
    stats = {"synced": 0, "skipped": 0, "errors": 0, "first_error": None}

    try:
        async with pool.acquire() as conn:
            # Ensure table exists (same schema as DT MCP worker)
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS digital_twins (
                    user_id TEXT PRIMARY KEY,
                    data JSONB NOT NULL DEFAULT '{}',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            ''')

            # Только пользователи с user_uuid (T1+ с Ory identity)
            rows = await conn.fetch('''
                SELECT
                    user_uuid,
                    sessions_total,
                    ai_chats_total,
                    marathon_steps_total,
                    marathon_tasks_total,
                    feed_completed_total,
                    training_attempts_total,
                    training_passed_total,
                    assessments_total,
                    events_total,
                    first_event_at,
                    last_event_at,
                    active_days,
                    events_last_7d,
                    events_last_30d
                FROM development.engagement
                WHERE user_uuid IS NOT NULL
            ''')

            for row in rows:
                try:
                    user_id = str(row['user_uuid'])
                    now_iso = datetime.now(timezone.utc).isoformat()

                    collected_data = {
                        "2_1_account": {
                            "sessions_total": row['sessions_total'],
                            "events_total": row['events_total'],
                            "first_event_at": _ts(row['first_event_at']),
                            "last_event_at": _ts(row['last_event_at']),
                        },
                        "2_2_courses": {
                            "marathon_steps_total": row['marathon_steps_total'],
                            "feed_completed_total": row['feed_completed_total'],
                        },
                        "2_3_practice": {
                            "training_attempts_total": row['training_attempts_total'],
                            "training_passed_total": row['training_passed_total'],
                            "assessments_total": row['assessments_total'],
                            "marathon_tasks_total": row['marathon_tasks_total'],
                        },
                        "2_4_time": {
                            "active_days": row['active_days'],
                            "events_last_7d": row['events_last_7d'],
                            "events_last_30d": row['events_last_30d'],
                            "ai_chats_total": row['ai_chats_total'],
                        },
                    }

                    # Deep merge: сохраняем существующие данные, обновляем 2_collected
                    await conn.execute('''
                        INSERT INTO digital_twins (user_id, data, created_at, updated_at)
                        VALUES ($1, jsonb_build_object('2_collected', $2::jsonb), NOW(), NOW())
                        ON CONFLICT (user_id) DO UPDATE SET
                            data = COALESCE(digital_twins.data, '{}'::jsonb)
                                || jsonb_build_object('2_collected',
                                    COALESCE(digital_twins.data->'2_collected', '{}'::jsonb)
                                    || $2::jsonb
                                ),
                            updated_at = NOW()
                    ''', user_id, json.dumps(collected_data))

                    stats["synced"] += 1
                except Exception as e:
                    logger.warning(f"[DT Sync] Failed for user {row['user_uuid']}: {e}")
                    stats["errors"] += 1
                    if not stats["first_error"]:
                        stats["first_error"] = f"{row['user_uuid']}: {e}"

    except Exception as e:
        logger.error(f"[DT Sync] Fatal error: {e}")
        stats["errors"] += 1
        if not stats["first_error"]:
            stats["first_error"] = str(e)

    logger.info(
        f"[DT Sync] Done: {stats['synced']} synced, "
        f"{stats['skipped']} skipped, {stats['errors']} errors"
    )
    return stats


async def get_engagement_data(user_uuid: str) -> dict | None:
    """Прочитать engagement проекции пользователя из digital_twins.

    Returns:
        dict с ключами 2_1_account..2_4_time или None если нет данных.
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data->'2_collected' AS collected FROM digital_twins WHERE user_id = $1",
                user_uuid,
            )
            if row and row['collected']:
                data = row['collected']
                return json.loads(data) if isinstance(data, str) else data
    except Exception as e:
        logger.warning(f"[DT Sync] get_engagement_data failed for {user_uuid}: {e}")
    return None


def _ts(val) -> str | None:
    """Конвертировать datetime в ISO string для JSONB."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)
