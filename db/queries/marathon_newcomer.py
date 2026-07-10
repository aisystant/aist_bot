from __future__ import annotations

"""
Запросы для нового марафона новичков (WP-330).

Отличается от legacy marathon_content (migration 200):
  - тексты читаются из файлов, не генерируются через Claude
  - очередь learning.marathon_queue (cron каждые 10 мин)
  - состояния learning.marathon_state (Хаос/Тупик/Поворот)
  - прогресс learning.marathon_progress
"""

from datetime import datetime, timedelta
from typing import Optional

from db.connection import get_learning_pool
from db.sql_helpers import update as _update_sql
from config import get_logger

logger = get_logger(__name__)


async def is_on_newcomer_marathon(user_id: int) -> bool:
    """True, если пользователь переведён на новый движок марафона (WP-330 cutover).

    Симметрично фильтру в get_all_scheduled_interns (db/queries/users.py): наличие
    строки в learning.marathon_progress = пользователь на новой системе. Для таких
    пользователей legacy-доставка (send_scheduled_topic) И legacy-напоминания
    (+1h/+3h, send_reminder) обязаны быть отключены — иначе два движка шлют
    параллельно (рассинхрон Block MAR: уроки «День 2» новым движком + напоминания
    «День 1 не начат» старым).
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            'SELECT 1 FROM learning.marathon_progress WHERE user_id = $1 LIMIT 1',
            user_id,
        )
    return exists is not None


async def get_pending_queue_items(limit: int = 100):
    """Выбрать pending-записи из очереди, которые пора отправлять."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT id, user_id, day_number, content_type, content_ref, content_text,
                      scheduled_at, attempts
               FROM learning.marathon_queue
               WHERE status = 'pending' AND scheduled_at <= NOW()
               ORDER BY scheduled_at
               LIMIT $1
               FOR UPDATE SKIP LOCKED''',
            limit,
        )
    return [dict(r) for r in rows]


async def mark_queue_sent(queue_id: int):
    """Отметить запись как отправленную."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''UPDATE learning.marathon_queue
               SET status = 'sent', sent_at = NOW(), updated_at = NOW()
               WHERE id = $1''',
            queue_id,
        )


async def schedule_queue_retry(queue_id: int, attempts: int, delay_minutes: int = 30):
    """Перенести отправку на delay_minutes вперёд."""
    scheduled_at = datetime.utcnow() + timedelta(minutes=delay_minutes)
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''UPDATE learning.marathon_queue
               SET status = 'pending',
                   attempts = $2,
                   scheduled_at = $3,
                   updated_at = NOW()
               WHERE id = $1''',
            queue_id, attempts + 1, scheduled_at,
        )


async def mark_queue_failed(queue_id: int, error: str):
    """Отметить запись как failed после исчерпания попыток."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''UPDATE learning.marathon_queue
               SET status = 'failed', error = $2, updated_at = NOW()
               WHERE id = $1''',
            queue_id, error[:500],
        )


async def has_active_marathon_progress(user_id: int) -> bool:
    """Проверить, есть ли активный марафон WP-330 у пользователя."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT 1 FROM learning.marathon_progress
               WHERE user_id = $1 AND status = 'active'
               LIMIT 1''',
            user_id,
        )
    return row is not None


async def get_sent_checkins_count(user_id: int) -> int:
    """Количество уже отправленных чек-инов пользователю.

    Используется для корректного расчёта missed_checkins (WP-330 B1):
    missed = sent_checkins - total_checkins (а не current_day - total_checkins).
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT COUNT(*) AS cnt
               FROM learning.marathon_queue
               WHERE user_id = $1
                 AND content_type = 'checkin'
                 AND status = 'sent' ''',
            user_id,
        )
    return row["cnt"] if row else 0


async def get_or_create_progress(user_id: int) -> dict:
    """Получить или создать прогресс участника."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT * FROM learning.marathon_progress WHERE user_id = $1''',
            user_id,
        )
        if row:
            return dict(row)
        await conn.execute(
            '''INSERT INTO learning.marathon_progress (user_id)
               VALUES ($1) ON CONFLICT DO NOTHING''',
            user_id,
        )
        row = await conn.fetchrow(
            '''SELECT * FROM learning.marathon_progress WHERE user_id = $1''',
            user_id,
        )
        return dict(row)


async def update_progress(
    user_id: int,
    current_day: Optional[int] = None,
    status: Optional[str] = None,
    started_at: Optional[datetime] = None,
    badge_list: Optional[list] = None,
    nudge_variant: Optional[str] = None,
):
    """Обновить прогресс участника."""
    pool = await get_learning_pool()
    fields = []
    values = []
    idx = 1

    if current_day is not None:
        fields.append(f"current_day = ${idx}")
        values.append(current_day)
        idx += 1
    if status is not None:
        fields.append(f"status = ${idx}")
        values.append(status)
        idx += 1
    if started_at is not None:
        fields.append(f"started_at = ${idx}")
        values.append(started_at)
        idx += 1
    if badge_list is not None:
        fields.append(f"badge_list = ${idx}")
        values.append(badge_list)
        idx += 1
    if nudge_variant is not None:
        fields.append(f"nudge_variant = ${idx}")
        values.append(nudge_variant)
        idx += 1

    if not fields:
        return

    # _update_sql numbers columns starting at $2; $1 is always the WHERE key (user_id).
    columns = [f.split(" = ", 1)[0] for f in fields]
    sql = _update_sql(
        'learning.marathon_progress', columns,
        'user_id = $1',
        extra_set=["updated_at = NOW()"],
    )
    async with pool.acquire() as conn:
        await conn.execute(sql, user_id, *values)


async def save_checkin(user_id: int, day: int, state: str, notes: Optional[str] = None) -> bool:
    """Сохранить ежедневный check-in состояния.

    Возвращает True если это первый чек-ин за день (INSERT), False если обновление (UPDATE).
    Атомарен — использует xmax=0 trick, исключает TOCTOU при двойном тапе.
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''INSERT INTO learning.marathon_state (user_id, day, state, notes)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id, day) DO UPDATE SET
                   state = EXCLUDED.state,
                   check_in_at = NOW(),
                   notes = COALESCE(EXCLUDED.notes, learning.marathon_state.notes)
               RETURNING (xmax = 0) AS is_new_insert''',
            user_id, day, state, notes,
        )
    return bool(row["is_new_insert"]) if row else True


async def save_marathon_activity(
    user_id: int,
    activity_date,
    action_type: str = "checkin",
    raw_count: int = 1,
):
    """Upsert факта активности в marathon_activity (календарный день).

    Peer-session 2026-06-03-16: идемпотентный upsert через DO UPDATE.
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''INSERT INTO learning.marathon_activity (user_id, activity_date, action_type, raw_count)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id, activity_date, action_type)
               DO UPDATE SET
                   raw_count = EXCLUDED.raw_count,
                   updated_at = NOW()''',
            user_id,
            activity_date,
            action_type,
            raw_count,
        )


async def get_missed_streak(user_id: int, working_days: list[int] | None = None) -> int:
    """Посчитать streak пропущенных рабочих дней до вчерашнего дня включительно.

    Peer-session 2026-06-03-16: compute-on-demand через generate_series + marathon_activity.
    working_days — дни недели (1=Mon..7=Sun); default все дни.
    """
    wd = working_days if working_days is not None else [1, 2, 3, 4, 5, 6, 7]
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''WITH user_progress AS (
                     SELECT COALESCE(started_at, created_at)::DATE AS start_date
                     FROM learning.marathon_progress
                     WHERE user_id = $1
                 ),
                 days_series AS (
                     SELECT generate_series(
                         start_date,
                         CURRENT_DATE - INTERVAL '1 day',
                         '1 day'
                     )::DATE AS d
                     FROM user_progress
                 ),
                 relevant_days AS (
                     SELECT d FROM days_series
                     WHERE EXTRACT(DOW FROM d)::int = ANY($2::int[])
                 ),
                 activity AS (
                     SELECT activity_date FROM learning.marathon_activity
                     WHERE user_id = $1
                 ),
                 missed AS (
                     SELECT d FROM relevant_days
                     WHERE NOT EXISTS (SELECT 1 FROM activity WHERE activity_date = d)
                 ),
                 last_active AS (
                     SELECT COALESCE(MAX(activity_date), '1970-01-01'::DATE) AS d
                     FROM activity
                 )
                 SELECT COUNT(*) AS missed_streak
                 FROM missed
                 WHERE d > (SELECT d FROM last_active)''',
            user_id,
            wd,
        )
    return int(row["missed_streak"]) if row else 0


async def get_checkins(user_id: int) -> list[dict]:
    """Получить все check-ins участника."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT day, state, check_in_at, notes
               FROM learning.marathon_state
               WHERE user_id = $1
               ORDER BY day''',
            user_id,
        )
    return [dict(r) for r in rows]


async def get_total_checkins_count(user_id: int) -> int:
    """Количество уникальных дней с чек-ином у участника.

    WP-330 P1: derived из marathon_state вместо инкрементной колонки.
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT COUNT(DISTINCT day) AS cnt
               FROM learning.marathon_state
               WHERE user_id = $1''',
            user_id,
        )
    return row["cnt"] if row else 0


async def get_checkin_for_day(user_id: int, day: int) -> dict | None:
    """Получить check-in за конкретный день. None если ещё не было."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT day, state, check_in_at, notes
               FROM learning.marathon_state
               WHERE user_id = $1 AND day = $2''',
            user_id, day,
        )
    return dict(row) if row else None


async def clear_marathon_queue(user_id: int):
    """Удалить все pending-записи из очереди марафона для пользователя."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''DELETE FROM learning.marathon_queue
               WHERE user_id = $1 AND status = 'pending' ''',
            user_id,
        )


async def clear_marathon_state(user_id: int):
    """Удалить все чек-ин записи участника. WP-330 Ф8.2.

    Вызывается из /marathon_stop и при перезапуске марафона в start_marathon_flow,
    чтобы новый марафон не наследовал записи прошлых тестов.
    Без этого первый реальный чек-ин не инкрементирует current_day
    (handler видит existing запись от прошлого старта и пропускает increment).
    total_checkins derived из marathon_state, см. get_total_checkins_count.
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''DELETE FROM learning.marathon_state WHERE user_id = $1''',
            user_id,
        )


async def pause_marathon(user_id: int) -> bool:
    """Поставить марафон на паузу (status='paused'), НЕ стирая очередь.

    В отличие от /marathon_stop (status='dropped' + clear_marathon_queue),
    пауза сохраняет прогресс и будущие уроки. Планировщик пропускает
    paused-пользователей (см. _get_paused_user_ids в scheduler).
    Возвращает True, если статус действительно сменился с active.
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            '''UPDATE learning.marathon_progress
               SET status = 'paused', updated_at = NOW()
               WHERE user_id = $1 AND status = 'active' ''',
            user_id,
        )
    return result and result != "UPDATE 0"


async def resume_marathon(user_id: int, schedule_time: str = "04:00") -> bool:
    """Снять паузу (status='active') и переставить оставшиеся уроки от завтра.

    Пересдвиг нужен, чтобы накопившиеся за паузу дни не свалились пачкой:
    каждый pending-день получает новую дату по порядку, начиная с завтра,
    в привычное время schedule_time. Check-in = +14 часов от урока.
    Возвращает True, если был на паузе.
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            '''UPDATE learning.marathon_progress
               SET status = 'active', updated_at = NOW()
               WHERE user_id = $1 AND status = 'paused' ''',
            user_id,
        )
        if not result or result == "UPDATE 0":
            return False
        await conn.execute(
            '''WITH ranked AS (
                   SELECT DISTINCT day_number,
                          DENSE_RANK() OVER (ORDER BY day_number) AS rn
                   FROM learning.marathon_queue
                   WHERE user_id = $1 AND status = 'pending'
               )
               UPDATE learning.marathon_queue q
               SET scheduled_at = CASE
                       WHEN q.content_type = 'lesson_practice'
                       THEN (CURRENT_DATE + r.rn)::timestamp + $2::time
                       ELSE (CURRENT_DATE + r.rn)::timestamp + $2::time + INTERVAL '14 hours'
                   END,
                   error = NULL,
                   updated_at = NOW()
               FROM ranked r
               WHERE q.user_id = $1 AND q.day_number = r.day_number
                 AND q.status = 'pending' ''',
            user_id, schedule_time,
        )
    return True


async def get_paused_user_ids() -> set[int]:
    """ID участников на паузе — планировщик пропускает их при доставке."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id FROM learning.marathon_progress WHERE status = 'paused'"
        )
    return {r['user_id'] for r in rows}


async def get_failed_queue_items(limit: int = 50):
    """Получить failed-записи из очереди для алертов наставникам."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT user_id, day_number, content_type, error, attempts
               FROM learning.marathon_queue
               WHERE status = 'failed'
               ORDER BY updated_at DESC
               LIMIT $1''',
            limit,
        )
    return [dict(r) for r in rows]


async def get_missed_checkin_users(min_days: int = 2, working_days: list[int] | None = None):
    """Получить активных участников, пропустивших чек-ин min_days+ дней подряд.

    Peer-session 2026-06-03-16: календарные даты через marathon_activity.
    working_days — дни недели (1=Mon..7=Sun); default все дни.
    """
    wd = working_days if working_days is not None else [1, 2, 3, 4, 5, 6, 7]
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''WITH active_users AS (
                     SELECT user_id,
                            COALESCE(started_at, created_at)::DATE AS start_date,
                            current_day
                     FROM learning.marathon_progress
                     WHERE status = 'active' AND current_day > 0
                 ),
                 days_series AS (
                     SELECT user_id, generate_series(
                         start_date,
                         CURRENT_DATE - INTERVAL '1 day',
                         '1 day'
                     )::DATE AS d
                     FROM active_users
                 ),
                 relevant_days AS (
                     SELECT user_id, d FROM days_series
                     WHERE EXTRACT(DOW FROM d)::int = ANY($2::int[])
                 ),
                 activity AS (
                     SELECT user_id, activity_date
                     FROM learning.marathon_activity
                     WHERE user_id IN (SELECT user_id FROM active_users)
                 ),
                 missed AS (
                     SELECT rd.user_id, rd.d
                     FROM relevant_days rd
                     LEFT JOIN activity a
                         ON a.user_id = rd.user_id AND a.activity_date = rd.d
                     WHERE a.user_id IS NULL
                 ),
                 last_active AS (
                     SELECT user_id, MAX(activity_date) AS last_d
                     FROM activity
                     GROUP BY user_id
                 ),
                 streaks AS (
                     SELECT m.user_id, COUNT(*) AS missed_streak
                     FROM missed m
                     LEFT JOIN last_active la ON la.user_id = m.user_id
                     WHERE m.d > COALESCE(la.last_d, '1970-01-01'::DATE)
                     GROUP BY m.user_id
                 ),
                 total_checkins AS (
                     SELECT user_id, COUNT(DISTINCT day) AS cnt
                     FROM learning.marathon_state
                     WHERE user_id IN (SELECT user_id FROM active_users)
                     GROUP BY user_id
                 )
                 SELECT au.user_id,
                        au.current_day,
                        COALESCE(tc.cnt, 0) AS total_checkins,
                        au.start_date AS started_at,
                        COALESCE(s.missed_streak, 0) AS missed
                 FROM active_users au
                 LEFT JOIN streaks s ON s.user_id = au.user_id
                 LEFT JOIN total_checkins tc ON tc.user_id = au.user_id
                 WHERE COALESCE(s.missed_streak, 0) >= $1''',
            min_days,
            wd,
        )
    return [dict(r) for r in rows]


async def get_users_for_nudge(limit: int = 100, working_days: list[int] | None = None) -> list[dict]:
    """Получить активных участников с пропусками чек-инов для nudge.

    Peer-session 2026-06-03-16: календарные даты через marathon_activity.
    """
    wd = working_days if working_days is not None else [1, 2, 3, 4, 5, 6, 7]
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''WITH active_users AS (
                     SELECT user_id,
                            COALESCE(started_at, created_at)::DATE AS start_date,
                            current_day
                     FROM learning.marathon_progress
                     WHERE status = 'active' AND current_day > 0
                 ),
                 days_series AS (
                     SELECT user_id, generate_series(
                         GREATEST(start_date, CURRENT_DATE - INTERVAL '30 days'),
                         CURRENT_DATE - INTERVAL '1 day',
                         '1 day'
                     )::DATE AS d
                     FROM active_users
                 ),
                 relevant_days AS (
                     SELECT user_id, d FROM days_series
                     WHERE EXTRACT(DOW FROM d)::int = ANY($2::int[])
                 ),
                 activity AS (
                     SELECT user_id, activity_date
                     FROM learning.marathon_activity
                     WHERE user_id IN (SELECT user_id FROM active_users)
                 ),
                 missed AS (
                     SELECT rd.user_id, rd.d
                     FROM relevant_days rd
                     LEFT JOIN activity a
                         ON a.user_id = rd.user_id AND a.activity_date = rd.d
                     WHERE a.user_id IS NULL
                 ),
                 last_active AS (
                     SELECT user_id, MAX(activity_date) AS last_d
                     FROM activity
                     GROUP BY user_id
                 ),
                 streaks AS (
                     SELECT m.user_id, COUNT(*) AS missed_streak
                     FROM missed m
                     LEFT JOIN last_active la ON la.user_id = m.user_id
                     WHERE m.d > COALESCE(la.last_d, '1970-01-01'::DATE)
                     GROUP BY m.user_id
                 ),
                 total_checkins AS (
                     SELECT user_id, COUNT(DISTINCT day) AS cnt
                     FROM learning.marathon_state
                     WHERE user_id IN (SELECT user_id FROM active_users)
                     GROUP BY user_id
                 )
                 SELECT au.user_id,
                        au.current_day,
                        COALESCE(tc.cnt, 0) AS total_checkins,
                        COALESCE(s.missed_streak, 0) AS missed
                 FROM active_users au
                 LEFT JOIN streaks s ON s.user_id = au.user_id
                 LEFT JOIN total_checkins tc ON tc.user_id = au.user_id
                 WHERE COALESCE(s.missed_streak, 0) >= 1
                 LIMIT $1''',
            limit,
            wd,
        )
    return [dict(r) for r in rows]


async def get_users_for_practice_nudge(limit: int = 100) -> list[dict]:
    """WP-330 Ф10.D: пользователи, получившие урок СЕГОДНЯ >30 мин назад,
    но не нажавшие «✏️ Перейти к практике» и не сделавшие чек-ин.

    Возвращает sent_at, чтобы планировщик мог определить окно:
    - 30–150 мин → первый нудж (:30m)
    - >150 мин   → второй и последний нудж (:150m)
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT mq.user_id, mq.day_number, mq.sent_at
               FROM learning.marathon_queue mq
               JOIN learning.marathon_progress mp ON mp.user_id = mq.user_id
               WHERE mq.content_type = 'lesson_practice'
                 AND mq.status = 'sent'
                 AND mq.sent_at::date = CURRENT_DATE
                 AND mq.sent_at <= NOW() - INTERVAL '30 minutes'
                 AND mp.status = 'active'
                 AND NOT EXISTS (
                   SELECT 1 FROM domain_event de
                   WHERE de.source = 'aist-bot'
                     AND de.event_type = 'notification_sent'
                     AND de.external_id =
                       'notification-marathon_practice:' || mq.user_id::text
                       || ':' || mq.day_number::text
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM learning.marathon_state ms
                   WHERE ms.user_id = mq.user_id AND ms.day = mq.day_number
                 )
               LIMIT $1''',
            limit,
        )
    return [dict(r) for r in rows]


async def get_active_marathon_users() -> list[dict]:
    """Получить всех активных участников марафона.

    WP-330 P1: total_checkins теперь derived из marathon_state.
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT mp.user_id, mp.current_day, mp.started_at,
                      (SELECT COUNT(DISTINCT day)
                       FROM learning.marathon_state ms
                       WHERE ms.user_id = mp.user_id
                      ) AS total_checkins
               FROM learning.marathon_progress mp
               WHERE mp.status = 'active' '''
        )
    return [dict(r) for r in rows]


async def has_recent_lesson_practice_sent(user_id: int, within_minutes: int = 60) -> bool:
    """Return True if a lesson_practice was recently sent to this user.

    Used by SM-mutex guard in external_session.py to detect marathon context
    when the scheduler delivered content without SM transition.
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''SELECT 1 FROM learning.marathon_queue
               WHERE user_id = $1
                 AND content_type = 'lesson_practice'
                 AND status = 'sent'
                 AND sent_at >= NOW() - ($2 * INTERVAL '1 minute')
               LIMIT 1''',
            user_id, within_minutes,
        )
    return row is not None


async def enqueue_day_items(user_id: int, day_number: int, scheduled_at: datetime, content_texts: dict | None = None):
    """Запланировать 2 отправки для одного дня марафона.

    Args:
        scheduled_at: базовое время (утро 04:00 MSK). Check-in = +14ч (18:00).
        content_texts: опционально {content_type: text} для предзаполнения content_text.
    """
    pool = await get_learning_pool()
    lesson_practice_text = (content_texts or {}).get('lesson_practice')
    checkin_text = (content_texts or {}).get('checkin')
    async with pool.acquire() as conn:
        await conn.execute(
            '''INSERT INTO learning.marathon_queue
               (user_id, day_number, content_type, scheduled_at, content_text)
               VALUES
               ($1, $2, 'lesson_practice', $3, $4),
               ($1, $2, 'checkin',         $3 + INTERVAL '14 hours', $5)
               ON CONFLICT (user_id, day_number, content_type) DO UPDATE SET
                   content_text = EXCLUDED.content_text,
                   scheduled_at = EXCLUDED.scheduled_at,
                   status = CASE WHEN marathon_queue.status = 'sent' THEN 'sent' ELSE 'pending' END,
                   sent_at = CASE WHEN marathon_queue.status = 'sent' THEN marathon_queue.sent_at ELSE NULL END,
                   error = NULL,
                   updated_at = NOW()''',
            user_id, day_number, scheduled_at, lesson_practice_text, checkin_text,
        )
