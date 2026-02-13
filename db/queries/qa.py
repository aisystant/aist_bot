"""
Запросы для истории вопросов и ответов.
"""

import json
from typing import List, Optional

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


async def save_qa(chat_id: int, mode: str, context_topic: str,
                  question: str, answer: str, mcp_sources: List[str] = None) -> Optional[int]:
    """Сохранить вопрос и ответ. Возвращает id записи."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            INSERT INTO qa_history
            (chat_id, mode, context_topic, question, answer, mcp_sources)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        ''', chat_id, mode, context_topic, question, answer,
            json.dumps(mcp_sources or []))
        return row['id'] if row else None


async def get_qa_history(chat_id: int, limit: int = 50) -> List[dict]:
    """Получить историю вопросов и ответов"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT * FROM qa_history
            WHERE chat_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        ''', chat_id, limit)

        return [{
            'id': row['id'],
            'mode': row['mode'],
            'context_topic': row['context_topic'],
            'question': row['question'],
            'answer': row['answer'],
            'mcp_sources': json.loads(row['mcp_sources']) if row['mcp_sources'] else [],
            'created_at': row['created_at']
        } for row in rows]


async def get_qa_by_id(qa_id: int) -> Optional[dict]:
    """Получить конкретный Q&A по ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM qa_history WHERE id = $1', qa_id
        )
        if not row:
            return None
        return {
            'id': row['id'],
            'chat_id': row['chat_id'],
            'mode': row['mode'],
            'context_topic': row['context_topic'],
            'question': row['question'],
            'answer': row['answer'],
            'mcp_sources': json.loads(row['mcp_sources']) if row['mcp_sources'] else [],
            'created_at': row['created_at']
        }


async def get_latest_qa_id(chat_id: int) -> Optional[int]:
    """Получить ID последнего Q&A для пользователя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id FROM qa_history WHERE chat_id = $1 ORDER BY created_at DESC LIMIT 1',
            chat_id
        )
        return row['id'] if row else None


async def update_qa_helpful(qa_id: int, helpful: bool):
    """Записать feedback (helpful/not helpful)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE qa_history SET helpful = $1 WHERE id = $2',
            helpful, qa_id
        )


async def update_qa_comment(qa_id: int, comment: str):
    """Записать замечание пользователя."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE qa_history SET user_comment = $1 WHERE id = $2',
            comment, qa_id
        )


async def get_qa_count(chat_id: int) -> int:
    """Получить количество заданных вопросов"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT COUNT(*) as count FROM qa_history WHERE chat_id = $1',
            chat_id
        )
        return row['count']
