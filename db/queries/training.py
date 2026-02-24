"""
DB-запросы для режима Тренировка (WP-55).

Таблицы: training_settings, training_progress, training_attempts.
"""

import json
from typing import Optional

from db.connection import get_pool
from config import get_logger

logger = get_logger(__name__)


# ============= SETTINGS =============

async def get_training_settings(chat_id: int) -> Optional[dict]:
    """Получить настройки тренировки пользователя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM training_settings WHERE chat_id = $1',
            chat_id
        )
        if not row:
            return None
        result = dict(row)
        result['enabled_principles'] = json.loads(result.get('enabled_principles') or '[]')
        return result


async def save_training_settings(
    chat_id: int,
    cognitive_level: str,
    enabled_principles: list
) -> dict:
    """Создать или обновить настройки тренировки."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            INSERT INTO training_settings (chat_id, cognitive_level, enabled_principles)
            VALUES ($1, $2, $3)
            ON CONFLICT (chat_id) DO UPDATE SET
                cognitive_level = EXCLUDED.cognitive_level,
                enabled_principles = EXCLUDED.enabled_principles,
                updated_at = NOW()
            RETURNING *
        ''', chat_id, cognitive_level, json.dumps(enabled_principles))
        result = dict(row)
        result['enabled_principles'] = json.loads(result.get('enabled_principles') or '[]')
        return result


async def update_training_settings(chat_id: int, **kwargs) -> None:
    """Обновить отдельные поля настроек."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if 'cognitive_level' in kwargs:
            await conn.execute(
                'UPDATE training_settings SET cognitive_level = $2, updated_at = NOW() WHERE chat_id = $1',
                chat_id, kwargs['cognitive_level']
            )
        if 'enabled_principles' in kwargs:
            await conn.execute(
                'UPDATE training_settings SET enabled_principles = $2, updated_at = NOW() WHERE chat_id = $1',
                chat_id, json.dumps(kwargs['enabled_principles'])
            )


# ============= PROGRESS =============

async def get_training_progress(chat_id: int) -> list:
    """Получить прогресс по всем принципам."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT * FROM training_progress WHERE chat_id = $1 ORDER BY principle_id',
            chat_id
        )
        return [dict(r) for r in rows]


async def get_principle_depth(chat_id: int, principle_id: str) -> int:
    """Получить текущую глубину принципа (0 = не начат)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            'SELECT current_depth FROM training_progress WHERE chat_id = $1 AND principle_id = $2',
            chat_id, principle_id
        )
        return val or 0


async def advance_principle_depth(chat_id: int, principle_id: str, new_depth: int) -> None:
    """Увеличить глубину принципа (UPSERT)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO training_progress (chat_id, principle_id, current_depth, attempts_at_depth, last_completed_at)
            VALUES ($1, $2, $3, 0, NOW())
            ON CONFLICT (chat_id, principle_id) DO UPDATE SET
                current_depth = $3,
                attempts_at_depth = 0,
                last_completed_at = NOW()
        ''', chat_id, principle_id, new_depth)


async def increment_attempts(chat_id: int, principle_id: str) -> None:
    """Увеличить счётчик попыток на текущей глубине."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO training_progress (chat_id, principle_id, current_depth, attempts_at_depth)
            VALUES ($1, $2, 1, 1)
            ON CONFLICT (chat_id, principle_id) DO UPDATE SET
                attempts_at_depth = training_progress.attempts_at_depth + 1
        ''', chat_id, principle_id)


# ============= ATTEMPTS =============

async def save_training_attempt(
    chat_id: int,
    principle_id: str,
    depth: int,
    assignment_text: str,
    answer_text: str,
    passed: bool,
    feedback: str,
) -> int:
    """Сохранить попытку ответа. Возвращает ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        attempt_id = await conn.fetchval('''
            INSERT INTO training_attempts
                (chat_id, principle_id, depth, assignment_text, answer_text, passed, feedback)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
        ''', chat_id, principle_id, depth, assignment_text, answer_text, passed, feedback)
        return attempt_id


async def get_training_stats(chat_id: int) -> dict:
    """Агрегированная статистика тренировки."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT
                COUNT(*) as total_attempts,
                COUNT(*) FILTER (WHERE passed = TRUE) as total_passed,
                COUNT(DISTINCT principle_id) FILTER (WHERE passed = TRUE) as principles_practiced
            FROM training_attempts
            WHERE chat_id = $1
        ''', chat_id)
        return dict(row) if row else {'total_attempts': 0, 'total_passed': 0, 'principles_practiced': 0}
