"""
Синхронизация engagement данных из Neon → digital_twins JSONB (WP-85, Phase 4).

Бот и DT MCP делят одну Neon БД. Sync job пишет напрямую в digital_twins,
минуя HTTP. DT MCP читает при запросе пользователя.

Секции 2_collected:
  2_1_account, 2_2_courses, 2_3_practice, 2_4_time — из development.engagement
  2_5_notifications — из development.notification_engagement (WP-152 Ф4)
  2_6_coding, 2_7_iwe — из dt-collect.sh (IWE-side)

Частота: ежедневно (scheduler cron).
"""

import json
import logging
from datetime import datetime, timezone

from db.connection import get_pool
from db.queries.dt_calc import calculate_derived

logger = logging.getLogger(__name__)


async def sync_engagement_to_dt() -> dict:
    """Синхронизировать engagement данные всех пользователей в digital_twins.

    Читает development.engagement + notification_engagement views,
    маппит на 5 групп метамодели (2_collected), подтягивает существующие
    2_6_coding/2_7_iwe из digital_twins (WP-174), вычисляет 3_derived
    (calculation engine v0.6), пишет в digital_twins.data JSONB.

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

            # ─── Notification engagement (WP-152 Ф4) ───
            # Предзагрузка: user_id → notification stats (для merge ниже)
            notif_map = {}
            try:
                notif_rows = await conn.fetch('''
                    SELECT
                        user_id,
                        notifications_total,
                        notifications_7d,
                        notifications_30d,
                        notification_types,
                        lesson_notifications,
                        reminder_notifications,
                        nudge_notifications,
                        trial_expiry_notifications,
                        feed_digest_notifications,
                        milestone_notifications,
                        first_notification_at,
                        last_notification_at
                    FROM development.notification_engagement
                ''')
                for nr in notif_rows:
                    notif_map[nr['user_id']] = nr
            except Exception as e:
                # View может не существовать на старых инстансах
                logger.warning(f"[DT Sync] notification_engagement not available: {e}")

            # ─── Learning history (WP-175 Ф5) ───
            # Предзагрузка: user_uuid → list of learning_history rows (v2 schema)
            # Используется для BKT: mastery_by_area, worldview_gaps
            learning_map: dict = {}
            try:
                lh_rows = await conn.fetch('''
                    SELECT
                        user_uuid::TEXT AS user_uuid,
                        element_id,
                        element_type,
                        area,
                        depth,
                        passed,
                        created_at
                    FROM development.learning_history
                    WHERE schema_version = 2
                      AND user_uuid IS NOT NULL
                      AND element_id IS NOT NULL
                    ORDER BY created_at DESC
                ''')
                for lr in lh_rows:
                    uid = lr['user_uuid']
                    if uid not in learning_map:
                        learning_map[uid] = []
                    learning_map[uid].append({
                        "element_id": lr['element_id'],
                        "element_type": lr['element_type'],
                        "area": lr['area'],
                        "depth": lr['depth'],
                        "passed": lr['passed'],
                    })
            except Exception as e:
                logger.warning(f"[DT Sync] learning_history not available: {e}")

            # Пользователи с user_uuid (T1+). Если есть dt_user_id (OAuth) —
            # писать по нему (worker ищет по этому ключу). Fallback на user_uuid.
            rows = await conn.fetch('''
                SELECT
                    e.user_uuid,
                    e.user_id,
                    dt.dt_user_id,
                    e.sessions_total,
                    e.ai_chats_total,
                    e.marathon_steps_total,
                    e.marathon_tasks_total,
                    e.feed_completed_total,
                    e.training_attempts_total,
                    e.training_passed_total,
                    e.assessments_total,
                    e.events_total,
                    e.first_event_at,
                    e.last_event_at,
                    e.active_days,
                    e.events_last_7d,
                    e.events_last_30d
                FROM development.engagement e
                LEFT JOIN dt_tokens dt ON dt.chat_id = e.user_id
                WHERE e.user_uuid IS NOT NULL
            ''')

            for row in rows:
                try:
                    # Prefer dt_user_id (Ory OAuth) — worker reads by this key
                    user_id = str(row['dt_user_id'] or row['user_uuid'])
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

                    # ─── 2_5_notifications (WP-152 Ф4) ───
                    # e.user_id = chat_id (telegram_id)
                    notif = notif_map.get(row['user_id'])
                    if notif:
                        collected_data["2_5_notifications"] = {
                            "notifications_total": notif['notifications_total'],
                            "notifications_7d": notif['notifications_7d'],
                            "notifications_30d": notif['notifications_30d'],
                            "notification_types": notif['notification_types'],
                            "lesson_notifications": notif['lesson_notifications'],
                            "reminder_notifications": notif['reminder_notifications'],
                            "nudge_notifications": notif['nudge_notifications'],
                            "trial_expiry_notifications": notif['trial_expiry_notifications'],
                            "feed_digest_notifications": notif['feed_digest_notifications'],
                            "milestone_notifications": notif['milestone_notifications'],
                            "first_notification_at": _ts(notif['first_notification_at']),
                            "last_notification_at": _ts(notif['last_notification_at']),
                        }

                    # ─── Merge existing 2_6/2_7 for builder path (WP-174) ───
                    # collected_data has 2_1..2_5 from engagement views.
                    # 2_6_coding and 2_7_iwe are written by dt-collect.sh
                    # and already in digital_twins. Merge them so
                    # calculate_derived() can use builder path thresholds.
                    existing = await conn.fetchval(
                        "SELECT data->'2_collected' FROM digital_twins WHERE user_id = $1",
                        user_id,
                    )
                    if existing:
                        existing_collected = json.loads(existing) if isinstance(existing, str) else existing
                        for key in ('2_6_coding', '2_7_iwe'):
                            if key in existing_collected and key not in collected_data:
                                collected_data[key] = existing_collected[key]

                    # ─── 3_derived (WP-151 Ф4, WP-174/175: calculation engine v0.7) ───
                    # learning_map key = user_uuid (str). Prefer dt_user_id for DT key,
                    # but learning_history is indexed by user_uuid.
                    lh_user_key = str(row['user_uuid'])
                    learning_rows = learning_map.get(lh_user_key)
                    derived_data = calculate_derived(collected_data, learning_rows)

                    # Deep merge: 2_collected + 3_derived в одну операцию
                    merge_payload = {
                        '2_collected': collected_data,
                    }
                    if derived_data:
                        merge_payload['3_derived'] = derived_data

                    await conn.execute('''
                        INSERT INTO digital_twins (user_id, data, created_at, updated_at)
                        VALUES ($1, $2::jsonb, NOW(), NOW())
                        ON CONFLICT (user_id) DO UPDATE SET
                            data = COALESCE(digital_twins.data, '{}'::jsonb)
                                || jsonb_build_object('2_collected',
                                    COALESCE(digital_twins.data->'2_collected', '{}'::jsonb)
                                    || ($2::jsonb->'2_collected')
                                )
                                || CASE WHEN $2::jsonb ? '3_derived'
                                    THEN jsonb_build_object('3_derived',
                                        COALESCE(digital_twins.data->'3_derived', '{}'::jsonb)
                                        || ($2::jsonb->'3_derived')
                                    )
                                    ELSE '{}'::jsonb
                                END,
                            updated_at = NOW()
                    ''', user_id, json.dumps(merge_payload))

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
        dict с ключами 2_1_account..2_5_notifications + 3_derived
        (3_derived вложен под ключом '_derived').
        None если нет данных.
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT data->'2_collected' AS collected,
                          data->'3_derived' AS derived
                   FROM digital_twins WHERE user_id = $1""",
                user_uuid,
            )
            if row and row['collected']:
                collected = row['collected']
                result = json.loads(collected) if isinstance(collected, str) else collected
                # Attach derived under '_derived' key
                if row['derived']:
                    derived = row['derived']
                    result['_derived'] = json.loads(derived) if isinstance(derived, str) else derived
                return result
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
