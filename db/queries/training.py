from __future__ import annotations

"""
DB-запросы для режима Тренировка (WP-55).

WP-253 lift-and-shift (8 мая 2026): таблицы вынесены из bot_data в Neon.
- learning БД: training_progress, training_attempt
- reference БД: training_setting, training_child
"""

import json
from typing import Optional

from db.connection import get_learning_pool, get_reference_pool
from config import get_logger

logger = get_logger(__name__)


# ============= SETTINGS =============

async def get_training_settings(chat_id: int) -> Optional[dict]:
    """Получить настройки тренировки пользователя."""
    pool = await get_reference_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM public.training_setting WHERE chat_id = $1',
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
    enabled_principles: list,
    training_mode: str = 'shuffle',
    single_principle: str = None,
) -> dict:
    """Создать или обновить настройки тренировки."""
    pool = await get_reference_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            INSERT INTO public.training_setting
                (chat_id, cognitive_level, enabled_principles, training_mode, single_principle)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (chat_id) DO UPDATE SET
                cognitive_level = EXCLUDED.cognitive_level,
                enabled_principles = EXCLUDED.enabled_principles,
                training_mode = EXCLUDED.training_mode,
                single_principle = EXCLUDED.single_principle,
                updated_at = NOW()
            RETURNING *
        ''', chat_id, cognitive_level, json.dumps(enabled_principles),
            training_mode, single_principle)
        result = dict(row)
        result['enabled_principles'] = json.loads(result.get('enabled_principles') or '[]')
        return result


async def update_training_settings(chat_id: int, **kwargs) -> None:
    """Обновить отдельные поля настроек."""
    pool = await get_reference_pool()
    async with pool.acquire() as conn:
        if 'cognitive_level' in kwargs:
            await conn.execute(
                'UPDATE public.training_setting SET cognitive_level = $2, updated_at = NOW() WHERE chat_id = $1',
                chat_id, kwargs['cognitive_level']
            )
        if 'enabled_principles' in kwargs:
            await conn.execute(
                'UPDATE public.training_setting SET enabled_principles = $2, updated_at = NOW() WHERE chat_id = $1',
                chat_id, json.dumps(kwargs['enabled_principles'])
            )
        if 'training_mode' in kwargs:
            await conn.execute(
                'UPDATE public.training_setting SET training_mode = $2, updated_at = NOW() WHERE chat_id = $1',
                chat_id, kwargs['training_mode']
            )
        if 'single_principle' in kwargs:
            await conn.execute(
                'UPDATE public.training_setting SET single_principle = $2, updated_at = NOW() WHERE chat_id = $1',
                chat_id, kwargs['single_principle']
            )


# ============= SETTINGS + PROGRESS (batch) =============

async def get_settings_and_progress(chat_id: int) -> Optional[tuple[dict, dict]]:
    """Получить настройки И прогресс двумя round-trip.

    WP-253 lift-and-shift: training_setting → reference БД, training_progress → learning БД.
    Cross-DB JOIN невозможен; поэтому два независимых запроса.

    Returns (settings_dict, progress_map) или None если настроек нет.
    progress_map: {principle_id: {current_depth, attempts_at_depth}}.
    """
    # 1) Settings из reference БД
    ref_pool = await get_reference_pool()
    async with ref_pool.acquire() as conn:
        settings_row = await conn.fetchrow('''
            SELECT cognitive_level, training_mode, single_principle, enabled_principles
            FROM public.training_setting
            WHERE chat_id = $1
        ''', chat_id)

    if not settings_row:
        return None

    settings = {
        'cognitive_level': settings_row['cognitive_level'],
        'training_mode': settings_row['training_mode'],
        'single_principle': settings_row['single_principle'],
        'enabled_principles': json.loads(settings_row['enabled_principles'] or '[]'),
    }

    # 2) Progress из learning БД
    learn_pool = await get_learning_pool()
    async with learn_pool.acquire() as conn:
        progress_rows = await conn.fetch('''
            SELECT principle_id, current_depth, attempts_at_depth
            FROM training_progress
            WHERE chat_id = $1 AND child_id IS NULL
            ORDER BY principle_id
        ''', chat_id)

    progress_map = {}
    for r in progress_rows:
        if r['principle_id']:
            progress_map[r['principle_id']] = {
                'principle_id': r['principle_id'],
                'current_depth': r['current_depth'],
                'attempts_at_depth': r['attempts_at_depth'],
            }

    return settings, progress_map


# ============= PROGRESS =============

async def get_training_progress(chat_id: int) -> list:
    """Получить прогресс по всем принципам."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT * FROM training_progress WHERE chat_id = $1 ORDER BY principle_id',
            chat_id
        )
        return [dict(r) for r in rows]


async def get_principle_depth(chat_id: int, principle_id: str) -> int:
    """Получить текущую глубину принципа (0 = не начат)."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            'SELECT current_depth FROM training_progress WHERE chat_id = $1 AND principle_id = $2',
            chat_id, principle_id
        )
        return val or 0


async def advance_principle_depth(chat_id: int, principle_id: str, new_depth: int) -> None:
    """Увеличить глубину принципа (UPSERT)."""
    pool = await get_learning_pool()
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
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO training_progress (chat_id, principle_id, current_depth, attempts_at_depth)
            VALUES ($1, $2, 1, 1)
            ON CONFLICT (chat_id, principle_id) DO UPDATE SET
                attempts_at_depth = training_progress.attempts_at_depth + 1
        ''', chat_id, principle_id)


async def reset_training_progress(chat_id: int) -> None:
    """Сбросить весь прогресс тренировки (начать заново)."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'DELETE FROM training_progress WHERE chat_id = $1', chat_id
        )
        await conn.execute(
            'DELETE FROM training_attempt WHERE chat_id = $1', chat_id
        )
    logger.info(f"[Training] Reset progress for chat_id={chat_id}")


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
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        attempt_id = await conn.fetchval('''
            INSERT INTO training_attempt
                (chat_id, principle_id, depth, assignment_text, answer_text, passed, feedback)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
        ''', chat_id, principle_id, depth, assignment_text, answer_text, passed, feedback)

        # ЦД: событие training_attempt (WP-85)
        from db.queries.events import log_event
        await log_event(chat_id, 'training_attempt', {
            'principle_id': principle_id,
            'depth': depth,
            'passed': passed,
            'attempt_id': attempt_id,
        }, confidence=0.9)

        return attempt_id


async def get_training_stats(chat_id: int, child_id: int = None) -> dict:
    """Агрегированная статистика тренировки."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        if child_id is not None:
            row = await conn.fetchrow('''
                SELECT
                    COUNT(*) as total_attempts,
                    COUNT(*) FILTER (WHERE passed = TRUE) as total_passed,
                    COUNT(DISTINCT principle_id) FILTER (WHERE passed = TRUE) as principles_practiced
                FROM training_attempt
                WHERE chat_id = $1 AND child_id = $2
            ''', chat_id, child_id)
        else:
            row = await conn.fetchrow('''
                SELECT
                    COUNT(*) as total_attempts,
                    COUNT(*) FILTER (WHERE passed = TRUE) as total_passed,
                    COUNT(DISTINCT principle_id) FILTER (WHERE passed = TRUE) as principles_practiced
                FROM training_attempt
                WHERE chat_id = $1 AND child_id IS NULL
            ''', chat_id)
        return dict(row) if row else {'total_attempts': 0, 'total_passed': 0, 'principles_practiced': 0}


# ============= CHILDREN (Phase 2) =============

async def create_training_child(chat_id: int, name: str, cognitive_level: str) -> dict:
    """Создать профиль ребёнка."""
    pool = await get_reference_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            INSERT INTO public.training_child (chat_id, name, cognitive_level)
            VALUES ($1, $2, $3)
            RETURNING *
        ''', chat_id, name, cognitive_level)
        return dict(row)


async def get_training_children(chat_id: int) -> list:
    """Получить список детей пользователя."""
    pool = await get_reference_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT * FROM public.training_child WHERE chat_id = $1 ORDER BY created_at',
            chat_id
        )
        return [dict(r) for r in rows]


async def get_training_child(child_id: int) -> Optional[dict]:
    """Получить профиль ребёнка по ID."""
    pool = await get_reference_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM public.training_child WHERE id = $1', child_id
        )
        return dict(row) if row else None


async def get_child_progress(chat_id: int, child_id: int) -> list:
    """Получить прогресс ребёнка по всем принципам."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT * FROM training_progress WHERE chat_id = $1 AND child_id = $2 ORDER BY principle_id',
            chat_id, child_id
        )
        return [dict(r) for r in rows]


async def get_child_principle_depth(chat_id: int, child_id: int, principle_id: str) -> int:
    """Получить текущую глубину принципа для ребёнка."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            'SELECT current_depth FROM training_progress WHERE chat_id = $1 AND child_id = $2 AND principle_id = $3',
            chat_id, child_id, principle_id
        )
        return val or 0


async def advance_child_principle_depth(chat_id: int, child_id: int, principle_id: str, new_depth: int) -> None:
    """Увеличить глубину принципа для ребёнка (UPSERT)."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO training_progress (chat_id, child_id, principle_id, current_depth, attempts_at_depth, last_completed_at)
            VALUES ($1, $2, $3, $4, 0, NOW())
            ON CONFLICT (chat_id, principle_id, child_id) WHERE child_id IS NOT NULL DO UPDATE SET
                current_depth = $4,
                attempts_at_depth = 0,
                last_completed_at = NOW()
        ''', chat_id, child_id, principle_id, new_depth)


async def save_child_training_attempt(
    chat_id: int,
    child_id: int,
    principle_id: str,
    depth: int,
    assignment_text: str,
    answer_text: str,
    passed: bool,
    feedback: str,
) -> int:
    """Сохранить попытку ответа ребёнка. Возвращает ID."""
    pool = await get_learning_pool()
    async with pool.acquire() as conn:
        attempt_id = await conn.fetchval('''
            INSERT INTO training_attempt
                (chat_id, child_id, principle_id, depth, assignment_text, answer_text, passed, feedback)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
        ''', chat_id, child_id, principle_id, depth, assignment_text, answer_text, passed, feedback)
        return attempt_id
