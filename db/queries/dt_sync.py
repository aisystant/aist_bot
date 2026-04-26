"""
Синхронизация engagement данных из Neon → digital_twins JSONB (WP-85, Phase 4).

Бот и DT MCP делят одну Neon БД. Sync job пишет напрямую в digital_twins,
минуя HTTP. DT MCP читает при запросе пользователя.

Секции 2_collected:
  2_1_account, 2_2_courses, 2_3_practice, 2_4_time — из development.engagement
  2_5_notifications — из development.notification_engagement (WP-152 Ф4)
  2_6_coding, 2_7_iwe — из development.user_events source='iwe' (ADR-009, WP-109 Ф3)
                         fallback: dt-collect.sh snapshot в digital_twins

Квалификация (WP-151 fix):
  2_2_courses.qualification_level — из LMS DB (qualification_level_event).
  Source-of-truth = Методсовет МИМ, не вычисляется.
  Связка: identity_map (lms user_id) → suser.email → contact → qualification_level_event.

Частота: ежедневно (scheduler cron).
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import asyncpg

from db.connection import get_pool, get_dt_pool
from helpers.dual_write import post_event
# WP-218 Ф2: calculator removed from bot — R28 Profiler is now single source
# of 3_derived. See DS-ai-systems/profiler/scripts/recalculate_derived.py
# (stand-alone runtime that reads digital_twins.data from Neon directly).
# WP-227: бот читает development.engagement из aist_bot (get_pool),
# но пишет digital_twins в digitaltwin БД (get_dt_pool). T0 не пишет в ЦД.

logger = logging.getLogger(__name__)


# ─── Маппинг LMS qualification level → код и числовой уровень ───
# Соответствует QualificationLevel.java в LMS
_QUAL_LEVEL_MAP = {
    "Интересант": {"code": "L05", "level": 5},
    "Определяющийся": {"code": "L08", "level": 8},
    "Первокурсник": {"code": "L1", "level": 10},
    "Ученик": {"code": "L2", "level": 20},
    "Работник": {"code": "L25", "level": 25},
    "Стратег": {"code": "L3", "level": 30},
    "Специалист": {"code": "L4", "level": 40},
    "Практик": {"code": "L5", "level": 50},
    "Мастер": {"code": "L6", "level": 60},
    "Реформатор": {"code": "L7", "level": 70},
    "Деятель (революционер)": {"code": "L8", "level": 80},
}


async def _preload_lms_qualifications(lms_user_ids: list[str]) -> dict:
    """Прочитать текущие квалификации из LMS DB для списка suser.id.

    Подключается к LMS PostgreSQL напрямую (LMS_DATABASE_URL).
    Возвращает {lms_user_id_str: {level, code, numeric, event_date, reason}}.
    При отсутствии LMS_DATABASE_URL — возвращает пустой dict (graceful).
    """
    lms_url = os.getenv("LMS_DATABASE_URL")
    if not lms_url:
        logger.info("[DT Sync] LMS_DATABASE_URL not set — skipping qualification sync")
        return {}

    if not lms_user_ids:
        return {}

    result = {}
    try:
        lms_conn = await asyncpg.connect(lms_url, timeout=10)
        try:
            # Текущая (максимальная) квалификация для каждого suser.
            # DISTINCT ON берёт последнюю по дате и максимальную по уровню.
            int_ids = []
            for uid in lms_user_ids:
                try:
                    int_ids.append(int(uid))
                except (ValueError, TypeError):
                    logger.warning(f"[DT Sync] Non-numeric LMS user_id skipped: {uid}")
            if not int_ids:
                return {}
            rows = await lms_conn.fetch('''
                SELECT DISTINCT ON (s.id)
                    s.id AS suser_id,
                    qle.level,
                    qle.event_date,
                    qle.reason
                FROM suser s
                JOIN contact c ON c.value = s.email AND c.contact_type = 0
                JOIN qualification_level_event qle ON qle.kontragent_id = c.kontragent_id
                WHERE s.id = ANY($1::bigint[])
                ORDER BY s.id, qle.event_date DESC, qle.id DESC
            ''', int_ids)

            for row in rows:
                level_name = row['level']
                meta = _QUAL_LEVEL_MAP.get(level_name, {})
                result[str(row['suser_id'])] = {
                    "level": level_name,
                    "code": meta.get("code", ""),
                    "numeric": meta.get("level", 0),
                    "event_date": row['event_date'].isoformat() if row['event_date'] else None,
                    "reason": row['reason'],
                }
        finally:
            await lms_conn.close()

    except Exception as e:
        logger.warning(f"[DT Sync] LMS qualification preload failed: {e}")

    logger.info(f"[DT Sync] Loaded {len(result)} qualifications from LMS DB")
    return result


async def sync_engagement_to_dt() -> dict:
    """Collector: sync engagement/LMS/coding/iwe данные в digital_twins.2_collected.

    WP-218 Ф2: бот — один из collectors, пишет только в 2_collected.
    Расчёт 3_derived выполняет R28 Profiler (standalone runtime):
        DS-ai-systems/profiler/scripts/recalculate_derived.py

    Что делает этот collector:
      - Читает development.engagement + notification_engagement views
      - Маппит на группы 2_1_account..2_5_notifications
      - Агрегирует 2_6_coding/2_7_iwe из user_events source='iwe' (WP-109 Ф3)
      - Подтягивает LMS qualification_level (WP-151 fix)
      - Пишет deep merge в digital_twins.data['2_collected']

    Что НЕ делает (вопреки ранней истории):
      - Не вызывает calculate_derived (ушло в profiler)
      - Не пишет в 3_derived (single writer — profiler)

    Returns:
        {"synced": N, "skipped": N, "errors": N, "first_error": str|None}
    """
    pool = await get_pool()
    dt_pool = await get_dt_pool()
    stats = {"synced": 0, "skipped": 0, "errors": 0, "first_error": None}

    try:
        async with pool.acquire() as conn, dt_pool.acquire() as dt_conn:

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

            # ─── LMS Qualifications (WP-151 fix) ───
            # Маппинг через users.aisystant_id (= LMS suser.id).
            # Не через identity_map — UUID там может отличаться от engagement UUID.
            # qual_by_chat_id: {telegram_id: qualification}
            qual_by_chat_id: dict = {}
            try:
                users_with_aisystant = await conn.fetch('''
                    SELECT telegram_id, aisystant_id
                    FROM public.users
                    WHERE aisystant_id IS NOT NULL AND aisystant_id != ''
                ''')
                aisystant_ids = [r['aisystant_id'] for r in users_with_aisystant]

                if aisystant_ids:
                    lms_quals = await _preload_lms_qualifications(aisystant_ids)
                    for r in users_with_aisystant:
                        qual = lms_quals.get(r['aisystant_id'])
                        if qual:
                            qual_by_chat_id[r['telegram_id']] = qual
                    logger.info(f"[DT Sync] Mapped {len(qual_by_chat_id)} qualifications via aisystant_id")
            except Exception as e:
                logger.warning(f"[DT Sync] LMS qualification mapping failed: {e}")

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

                    # ─── LMS Qualification → 2_2_courses (WP-151 fix) ───
                    qual = qual_by_chat_id.get(row['user_id'])
                    if qual:
                        collected_data["2_2_courses"]["qualification_level"] = qual

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

                    # ─── 2_8_decisions from user_events (WP-109 Ф7, PD.SOTA.003) ───
                    decision_stats = await conn.fetchrow('''
                        SELECT
                            COUNT(*) AS decisions_today,
                            COALESCE(SUM((payload->>'weight')::int), 0) AS decision_weight_today
                        FROM development.user_events
                        WHERE user_uuid = $1::uuid
                          AND source = 'exocortex'
                          AND event_type LIKE 'decision_%'
                          AND created_at >= CURRENT_DATE
                    ''', user_id)

                    decision_7d = await conn.fetchrow('''
                        SELECT
                            COALESCE(
                                AVG(daily_weight), 0
                            )::int AS decision_weight_7d_avg
                        FROM (
                            SELECT DATE(created_at) AS d,
                                   SUM((payload->>'weight')::int) AS daily_weight
                            FROM development.user_events
                            WHERE user_uuid = $1::uuid
                              AND source = 'exocortex'
                              AND event_type LIKE 'decision_%'
                              AND created_at >= NOW() - INTERVAL '7 days'
                            GROUP BY DATE(created_at)
                        ) daily
                    ''', user_id)

                    if decision_stats and decision_stats['decisions_today'] > 0:
                        collected_data['2_8_decisions'] = {
                            'decisions_today': decision_stats['decisions_today'],
                            'decision_weight_today': decision_stats['decision_weight_today'],
                            'decision_weight_7d_avg': decision_7d['decision_weight_7d_avg'] if decision_7d else 0,
                        }

                    # WP-218 Ф2: бот — только collector (писатель 2_collected).
                    # Расчёт 3_derived делает R28 Profiler (standalone runtime).
                    # WP-227: пишем в digitaltwin БД через dt_conn (только T1+).
                    merge_payload = {
                        '2_collected': collected_data,
                    }

                    await dt_conn.execute('''
                        INSERT INTO digital_twins (user_id, data, created_at, updated_at)
                        VALUES ($1, $2::jsonb, NOW(), NOW())
                        ON CONFLICT (user_id) DO UPDATE SET
                            data = COALESCE(digital_twins.data, '{}'::jsonb)
                                || jsonb_build_object('2_collected',
                                    COALESCE(digital_twins.data->'2_collected', '{}'::jsonb)
                                    || ($2::jsonb->'2_collected')
                                ),
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

    # WP-268 Phase 2 dual-write: bulk dt_recalc событие на весь батч
    # external_id привязан к "началу" cron-запуска, чтобы повторный запуск
    # с теми же stats был идемпотентен на уровне дня.
    try:
        now = datetime.utcnow()
        day_bucket = now.strftime("%Y-%m-%dT%H")  # часовой бакет — cron 04:30 MSK раз в день
        asyncio.create_task(post_event(
            source="aist-bot",
            external_id=f"dt-recalc-bulk-{day_bucket}",
            event_type="dt_recalc",
            schema_version="v1",
            occurred_at=now,
            account_id=None,  # bulk — много пользователей, конкретного ory_id нет
            payload={
                "mode": "bulk",
                "synced": stats.get("synced", 0),
                "skipped": stats.get("skipped", 0),
                "errors": stats.get("errors", 0),
            },
        ))
    except Exception as exc:
        logger.warning(f"[dual-write] dt_recalc bulk fire failed: {exc}")

    return stats


# WP-218 Ф2: get_engagement_data() удалена — была точкой нарушения
# единой точки чтения (handler'ы ходили через user_uuid из development.engagement,
# который может быть неоднозначным для chat_id, давая устаревшие данные).
# Бот читает ЦД ровно одним способом — через gateway_mcp.get_user_profile().


async def sync_one_user_to_dt(user_id: str) -> bool:
    """On-demand collector для одного пользователя (WP-175 Ф9-B).

    Используется после GitHub webhook: ученик закончил занятие → коммит в workbook/
    → Activity Hub → вызов этой функции → 2_collected обновлён.

    WP-218 Ф2: расчёт 3_derived НЕ выполняется здесь. Для on-demand пересчёта
    после обновления collected — вызывать R28 Profiler отдельно:
        python3 profiler/scripts/recalculate_derived.py --user-id <user_id>
    (TODO сессия B: триггер через subprocess / event bus / cron-tick).

    Логика идентична одной итерации sync_engagement_to_dt(), но принимает
    Ory UUID напрямую.

    Args:
        user_id: Ory UUID пользователя (ключ в digital_twins.user_id)

    Returns:
        True если collect прошёл успешно, False при ошибке.
    """
    pool = await get_pool()
    dt_pool = await get_dt_pool()
    now = datetime.now(timezone.utc)

    try:
        async with pool.acquire() as conn, dt_pool.acquire() as dt_conn:
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

            # ─── LMS Qualification for single user (WP-151 fix) ───
            try:
                user_row = await conn.fetchrow('''
                    SELECT aisystant_id FROM public.users
                    WHERE telegram_id = $1
                      AND aisystant_id IS NOT NULL AND aisystant_id != ''
                ''', row['user_id'])
                if user_row:
                    quals = await _preload_lms_qualifications([user_row['aisystant_id']])
                    qual = quals.get(user_row['aisystant_id'])
                    if qual:
                        collected_data["2_2_courses"]["qualification_level"] = qual
            except Exception as e:
                logger.warning(f"[DT Sync] single-user qualification failed: {e}")

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

            # WP-218 Ф2: бот — только collector. Расчёт 3_derived — в R28 Profiler
            # (DS-ai-systems/profiler/scripts/recalculate_derived.py).
            # On-demand пересчёт после webhook-занятия (WP-175 Ф9-B) теперь должен
            # триггерить profiler separately — TODO в сессии B:
            # заменить этот путь на вызов recalculate_derived --user-id ...
            merge_payload = {'2_collected': collected_data}

            # WP-227: пишем в digitaltwin БД через dt_conn
            await dt_conn.execute('''
                INSERT INTO digital_twins (user_id, data, created_at, updated_at)
                VALUES ($1, $2::jsonb, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    data = COALESCE(digital_twins.data, '{}'::jsonb)
                        || jsonb_build_object('2_collected',
                            COALESCE(digital_twins.data->'2_collected', '{}'::jsonb)
                            || ($2::jsonb->'2_collected')
                        ),
                    updated_at = NOW()
            ''', effective_user_id, json.dumps(merge_payload))

            logger.info(f"[DT Sync] sync_one_user done: {effective_user_id}")

            # WP-268 Phase 2 dual-write: per-user dt_recalc
            # Audit fix (Phase 2): epoch_ns заменён на hourly day-bucket (как в bulk
            # выше) — webhook retry в течение часа идемпотентен (один external_id).
            now = datetime.utcnow()
            day_bucket = now.strftime("%Y-%m-%dT%H")
            try:
                asyncio.create_task(post_event(
                    source="aist-bot",
                    external_id=f"dt-recalc-{effective_user_id}-{day_bucket}",
                    event_type="dt_recalc",
                    schema_version="v1",
                    occurred_at=now,
                    account_id=str(effective_user_id),  # это Ory UUID
                    payload={
                        "mode": "single",
                        "user_id": str(effective_user_id),
                        "sections_written": list(collected_data.keys()) if collected_data else [],
                    },
                ))
            except Exception as exc:
                logger.warning(f"[dual-write] dt_recalc single fire failed: {exc}")

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
