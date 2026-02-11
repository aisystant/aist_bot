"""
Запросы для работы с оценками (таблица assessments).
"""

import json
from typing import Optional, List

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


async def save_assessment(
    chat_id: int,
    assessment_id: str,
    answers: dict,
    scores: dict,
    dominant_state: str,
    self_check: str = None,
    open_response: str = None,
) -> Optional[int]:
    """Сохранить результат теста.

    Args:
        chat_id: ID пользователя
        assessment_id: ID теста (например, 'systematicity')
        answers: {question_id: True/False}
        scores: {group_id: score}
        dominant_state: ID преобладающей группы
        self_check: ответ на самооценку
        open_response: текст открытого вопроса

    Returns:
        ID записи или None
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''INSERT INTO assessments
               (chat_id, assessment_id, answers, scores,
                dominant_state, self_check, open_response)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING id''',
            chat_id,
            assessment_id,
            json.dumps(answers),
            json.dumps(scores),
            dominant_state,
            self_check,
            open_response,
        )
        return row['id'] if row else None


async def get_latest_assessment(
    chat_id: int, assessment_id: str = None
) -> Optional[dict]:
    """Получить последний результат теста.

    Args:
        chat_id: ID пользователя
        assessment_id: фильтр по типу теста (опционально)

    Returns:
        Словарь с результатом или None
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if assessment_id:
            row = await conn.fetchrow(
                '''SELECT * FROM assessments
                   WHERE chat_id = $1 AND assessment_id = $2
                   ORDER BY created_at DESC LIMIT 1''',
                chat_id, assessment_id,
            )
        else:
            row = await conn.fetchrow(
                '''SELECT * FROM assessments
                   WHERE chat_id = $1
                   ORDER BY created_at DESC LIMIT 1''',
                chat_id,
            )

        if not row:
            return None

        return {
            'id': row['id'],
            'chat_id': row['chat_id'],
            'assessment_id': row['assessment_id'],
            'answers': json.loads(row['answers']) if row['answers'] else {},
            'scores': json.loads(row['scores']) if row['scores'] else {},
            'dominant_state': row['dominant_state'],
            'self_check': row['self_check'],
            'open_response': row['open_response'],
            'created_at': row['created_at'],
        }


async def get_assessment_history(
    chat_id: int, assessment_id: str = None, limit: int = 10
) -> List[dict]:
    """Получить историю тестов.

    Args:
        chat_id: ID пользователя
        assessment_id: фильтр по типу теста (опционально)
        limit: максимум записей

    Returns:
        Список результатов
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if assessment_id:
            rows = await conn.fetch(
                '''SELECT * FROM assessments
                   WHERE chat_id = $1 AND assessment_id = $2
                   ORDER BY created_at DESC LIMIT $3''',
                chat_id, assessment_id, limit,
            )
        else:
            rows = await conn.fetch(
                '''SELECT * FROM assessments
                   WHERE chat_id = $1
                   ORDER BY created_at DESC LIMIT $2''',
                chat_id, limit,
            )

        return [
            {
                'id': r['id'],
                'assessment_id': r['assessment_id'],
                'scores': json.loads(r['scores']) if r['scores'] else {},
                'dominant_state': r['dominant_state'],
                'self_check': r['self_check'],
                'created_at': r['created_at'],
            }
            for r in rows
        ]
