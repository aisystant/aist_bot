"""
Синхронизация engagement данных из Neon → digital_twins JSONB (WP-85, Phase 4).

Бот и DT MCP делят одну Neon БД. Sync job пишет напрямую в digital_twins,
минуя HTTP. DT MCP читает при запросе пользователя.

Секции 2_collected:
  2_1_account, 2_2_courses, 2_3_practice, 2_4_time — из development.engagement
  2_5_notifications — из development.notification_engagement (WP-152 Ф4)
  2_6_coding, 2_7_iwe — из development.user_events source='iwe' (ADR-009, WP-109 Ф3)
                         fallback: dt-collect.sh snapshot в digital_twins

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
    маппит на 5 групп метамодели (2_collected), агрегирует 2_6_coding/2_7_iwe
    из user_events source='iwe' (ADR-009, WP-109 Ф3), вычисляет 3_derived
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
                    e.onboarding_completed_total,
                    e.mode_changes_total,
                    e.settings_changes_total,
                    e.reminders_delivered_total,
                    e.reminders_opened_total,
                    e.errors_shown_total,
                    e.help_views_total,
                    e.progress_views_total,
                    e.marathon_completions_total,
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

                    # ─── 2_8_operations (WP-151 Ф3) ───
                    collected_data["2_8_operations"] = {
                        "onboarding_completed": row['onboarding_completed_total'],
                        "mode_changes": row['mode_changes_total'],
                        "settings_changes": row['settings_changes_total'],
                        "reminders_delivered": row['reminders_delivered_total'],
                        "reminders_opened": row['reminders_opened_total'],
                        "errors_shown": row['errors_shown_total'],
                        "help_views": row['help_views_total'],
                        "progress_views": row['progress_views_total'],
                        "marathon_completions": row['marathon_completions_total'],
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

                    # ─── 2_6_coding from user_events (ADR-009, WP-109 Ф3) ───
                    # Агрегация coding_time и commit данных из user_events
                    # вместо подтягивания из digital_twins (dt-collect).
                    iwe_stats = await conn.fetchrow('''
                        SELECT
                            COALESCE(SUM(CASE
                                WHEN event_type = 'coding_time'
                                AND created_at >= NOW() - INTERVAL '1 day'
                                THEN (payload->>'total_seconds')::numeric::int
                            END), 0) AS coding_seconds_today,
                            COALESCE(SUM(CASE
                                WHEN event_type = 'coding_time'
                                AND created_at >= NOW() - INTERVAL '7 days'
                                THEN (payload->>'total_seconds')::numeric::int
                            END), 0) AS coding_seconds_7d,
                            COALESCE(SUM(CASE
                                WHEN event_type = 'coding_time'
                                AND created_at >= NOW() - INTERVAL '30 days'
                                THEN (payload->>'total_seconds')::numeric::int
                            END), 0) AS coding_seconds_30d,
                            COUNT(DISTINCT CASE
                                WHEN event_type = 'coding_time'
                                AND created_at >= NOW() - INTERVAL '30 days'
                                THEN DATE(created_at)
                            END) AS coding_active_days_30d,
                            COUNT(CASE
                                WHEN event_type = 'commit_created'
                                AND created_at >= NOW() - INTERVAL '1 day'
                                THEN 1
                            END) AS commits_today,
                            COUNT(CASE
                                WHEN event_type = 'commit_created'
                                AND created_at >= NOW() - INTERVAL '7 days'
                                THEN 1
                            END) AS commits_7d,
                            COUNT(CASE
                                WHEN event_type = 'commit_created'
                                AND created_at >= NOW() - INTERVAL '30 days'
                                THEN 1
                            END) AS commits_30d,
                            COUNT(DISTINCT CASE
                                WHEN event_type = 'day_open'
                                AND created_at >= NOW() - INTERVAL '30 days'
                                THEN DATE(created_at)
                            END) AS day_opens_30d
                        FROM development.user_events
                        WHERE user_uuid = $1::uuid
                          AND source = 'iwe'
                          AND created_at >= NOW() - INTERVAL '30 days'
                    ''', user_id)

                    if iwe_stats and iwe_stats['coding_seconds_30d'] > 0:
                        collected_data['2_6_coding'] = {
                            'coding_seconds_today': iwe_stats['coding_seconds_today'],
                            'coding_seconds_7d': iwe_stats['coding_seconds_7d'],
                            'coding_seconds_30d': iwe_stats['coding_seconds_30d'],
                            'coding_active_days_30d': iwe_stats['coding_active_days_30d'],
                        }

                    if iwe_stats and iwe_stats['commits_30d'] > 0:
                        collected_data.setdefault('2_7_iwe', {}).update({
                            'commits_today': iwe_stats['commits_today'],
                            'commits_7d': iwe_stats['commits_7d'],
                            'commits_30d': iwe_stats['commits_30d'],
                            'day_opens_30d': iwe_stats['day_opens_30d'],
                        })

                    # Fallback: если user_events пусто, подтянуть из digital_twins
                    # (dt-collect snapshot, переходный период)
                    if '2_6_coding' not in collected_data or '2_7_iwe' not in collected_data:
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


async def sync_one_user_to_dt(user_id: str) -> bool:
    """On-demand синхронизация одного пользователя в digital_twins (WP-175 Ф9-B).

    Используется после GitHub webhook: ученик закончил занятие → коммит в workbook/
    → Activity Hub → вызов этой функции → ЦД пересчитан прямо сейчас.

    Логика идентична одной итерации sync_engagement_to_dt(), но:
      - принимает Ory UUID напрямую (не итерирует по всем)
      - передаёт as_of=now в calculate_derived() для детерминированного расчёта

    Args:
        user_id: Ory UUID пользователя (ключ в digital_twins.user_id)

    Returns:
        True если синхронизация прошла успешно, False при ошибке.
    """
    pool = await get_pool()
    now = datetime.now(timezone.utc)

    try:
        async with pool.acquire() as conn:
            # Находим пользователя по dt_user_id (Ory UUID) или user_uuid
            row = await conn.fetchrow('''
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
                    e.onboarding_completed_total,
                    e.mode_changes_total,
                    e.settings_changes_total,
                    e.reminders_delivered_total,
                    e.reminders_opened_total,
                    e.errors_shown_total,
                    e.help_views_total,
                    e.progress_views_total,
                    e.marathon_completions_total,
                    e.events_total,
                    e.first_event_at,
                    e.last_event_at,
                    e.active_days,
                    e.events_last_7d,
                    e.events_last_30d
                FROM development.engagement e
                LEFT JOIN dt_tokens dt ON dt.chat_id = e.user_id
                WHERE e.user_uuid IS NOT NULL
                  AND (dt.dt_user_id = $1 OR e.user_uuid::TEXT = $1)
                LIMIT 1
            ''', user_id)

            if not row:
                logger.warning(f"[DT Sync] sync_one_user: user not found: {user_id}")
                return False

            # Notification engagement
            notif = None
            try:
                notif = await conn.fetchrow('''
                    SELECT * FROM development.notification_engagement
                    WHERE user_id = $1
                ''', row['user_id'])
            except Exception as e:
                logger.warning(f"[DT Sync] notification_engagement not available: {e}")

            # Learning history
            learning_rows = None
            try:
                lh_rows = await conn.fetch('''
                    SELECT
                        user_uuid::TEXT AS user_uuid,
                        element_id, element_type, area, depth, passed, created_at
                    FROM development.learning_history
                    WHERE schema_version = 2
                      AND user_uuid::TEXT = $1
                      AND element_id IS NOT NULL
                    ORDER BY created_at DESC
                ''', str(row['user_uuid']))
                learning_rows = [
                    {
                        "element_id": lr['element_id'],
                        "element_type": lr['element_type'],
                        "area": lr['area'],
                        "depth": lr['depth'],
                        "passed": lr['passed'],
                    }
                    for lr in lh_rows
                ]
            except Exception as e:
                logger.warning(f"[DT Sync] learning_history not available: {e}")

            effective_user_id = str(row['dt_user_id'] or row['user_uuid'])

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

            # ─── 2_6_coding from user_events (ADR-009, WP-109 Ф3) ───
            iwe_stats = await conn.fetchrow('''
                SELECT
                    COALESCE(SUM(CASE
                        WHEN event_type = 'coding_time'
                        AND created_at >= NOW() - INTERVAL '1 day'
                        THEN (payload->>'total_seconds')::numeric::int
                    END), 0) AS coding_seconds_today,
                    COALESCE(SUM(CASE
                        WHEN event_type = 'coding_time'
                        AND created_at >= NOW() - INTERVAL '7 days'
                        THEN (payload->>'total_seconds')::numeric::int
                    END), 0) AS coding_seconds_7d,
                    COALESCE(SUM(CASE
                        WHEN event_type = 'coding_time'
                        AND created_at >= NOW() - INTERVAL '30 days'
                        THEN (payload->>'total_seconds')::numeric::int
                    END), 0) AS coding_seconds_30d,
                    COUNT(DISTINCT CASE
                        WHEN event_type = 'coding_time'
                        AND created_at >= NOW() - INTERVAL '30 days'
                        THEN DATE(created_at)
                    END) AS coding_active_days_30d,
                    COUNT(CASE
                        WHEN event_type = 'commit_created'
                        AND created_at >= NOW() - INTERVAL '1 day'
                        THEN 1
                    END) AS commits_today,
                    COUNT(CASE
                        WHEN event_type = 'commit_created'
                        AND created_at >= NOW() - INTERVAL '7 days'
                        THEN 1
                    END) AS commits_7d,
                    COUNT(CASE
                        WHEN event_type = 'commit_created'
                        AND created_at >= NOW() - INTERVAL '30 days'
                        THEN 1
                    END) AS commits_30d,
                    COUNT(DISTINCT CASE
                        WHEN event_type = 'day_open'
                        AND created_at >= NOW() - INTERVAL '30 days'
                        THEN DATE(created_at)
                    END) AS day_opens_30d
                FROM development.user_events
                WHERE user_uuid = $1::uuid
                  AND source = 'iwe'
                  AND created_at >= NOW() - INTERVAL '30 days'
            ''', effective_user_id)

            if iwe_stats and iwe_stats['coding_seconds_30d'] > 0:
                collected_data['2_6_coding'] = {
                    'coding_seconds_today': iwe_stats['coding_seconds_today'],
                    'coding_seconds_7d': iwe_stats['coding_seconds_7d'],
                    'coding_seconds_30d': iwe_stats['coding_seconds_30d'],
                    'coding_active_days_30d': iwe_stats['coding_active_days_30d'],
                }

            if iwe_stats and iwe_stats['commits_30d'] > 0:
                collected_data.setdefault('2_7_iwe', {}).update({
                    'commits_today': iwe_stats['commits_today'],
                    'commits_7d': iwe_stats['commits_7d'],
                    'commits_30d': iwe_stats['commits_30d'],
                    'day_opens_30d': iwe_stats['day_opens_30d'],
                })

            # Fallback: dt-collect snapshot (переходный период)
            if '2_6_coding' not in collected_data or '2_7_iwe' not in collected_data:
                existing = await conn.fetchval(
                    "SELECT data->'2_collected' FROM digital_twins WHERE user_id = $1",
                    effective_user_id,
                )
                if existing:
                    existing_collected = json.loads(existing) if isinstance(existing, str) else existing
                    for key in ('2_6_coding', '2_7_iwe'):
                        if key in existing_collected and key not in collected_data:
                            collected_data[key] = existing_collected[key]

            # as_of фиксирует момент времени — детерминированный расчёт (WP-175 Ф9-B)
            derived_data = calculate_derived(collected_data, learning_rows, as_of=now)

            merge_payload = {'2_collected': collected_data}
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
            ''', effective_user_id, json.dumps(merge_payload))

            logger.info(f"[DT Sync] sync_one_user done: {effective_user_id}")
            return True

    except Exception as e:
        logger.error(f"[DT Sync] sync_one_user failed for {user_id}: {e}")
        return False


def _ts(val) -> str | None:
    """Конвертировать datetime в ISO string для JSONB."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)
