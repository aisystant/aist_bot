from __future__ import annotations

"""
Запросы для нового марафона новичков (WP-330).

Отличается от legacy marathon_content (migration 200):
  - тексты читаются из файлов, не генерируются через Claude
  - очередь learning.marathon_queue (cron каждые 10 мин)
  - состояния learning.marathon_state (Хаос/Тупик/Поворот)
  - прогресс learning.marathon_progress
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from db.connection import get_learning_pool
from config import get_logger

logger = get_logger(__name__)


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
    scheduled_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
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
    total_checkins: Optional[int] = None,
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
    if total_checkins is not None:
        fields.append(f"total_checkins = ${idx}")
        values.append(total_checkins)
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

    values.append(user_id)
    sql = f"UPDATE learning.marathon_progress SET {', '.join(fields)}, updated_at = NOW() WHERE user_id = ${idx}"
    async with pool.acquire() as conn:
        await conn.execute(sql, *values)


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
    Без этого первый реальный чек-ин не инкрементирует current_day/total_checkins
    (handler видит existing запись от прошлого старта и пропускает increment).
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            '''DELETE FROM learning.marathon_state WHERE user_id = $1''',
            user_id,
        )


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


async def get_missed_checkin_users(min_days: int = 2):
    """Получить активных участников, пропустивших чек-ин min_days+ дней подряд.

    Логика: current_day - total_checkins >= min_days
    (если current_day=3, total_checkins=0 → 3 дня без чек-ина)
    """
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT user_id, current_day, total_checkins, started_at
               FROM learning.marathon_progress
               WHERE status = 'active'
                 AND current_day - total_checkins >= $1
                 AND current_day > 0''',
            min_days,
        )
    return [dict(r) for r in rows]


async def get_users_for_nudge(limit: int = 100) -> list[dict]:
    """Получить активных участников с пропусками чек-инов для nudge."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT user_id, current_day, total_checkins
               FROM learning.marathon_progress
               WHERE status = 'active'
                 AND current_day > total_checkins
                 AND current_day > 0
               LIMIT $1''',
            limit,
        )
    return [dict(r) for r in rows]


async def get_active_marathon_users() -> list[dict]:
    """Получить всех активных участников марафона."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''SELECT user_id, current_day, total_checkins, started_at
               FROM learning.marathon_progress
               WHERE status = 'active' '''
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
                   status = 'pending',
                   sent_at = NULL,
                   error = NULL,
                   updated_at = NOW()''',
            user_id, day_number, scheduled_at, lesson_practice_text, checkin_text,
        )
