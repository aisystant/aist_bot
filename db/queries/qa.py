"""
Запросы для истории вопросов и ответов.
"""

import json
from typing import List, Optional

from config import get_logger
from db.connection import get_pool

logger = get_logger(__name__)


async def save_qa(chat_id: int, mode: str, context_topic: str,
                  question: str, answer: str, mcp_sources: List[str] = None):
    """Сохранить вопрос и ответ"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO qa_history
            (chat_id, mode, context_topic, question, answer, mcp_sources)
            VALUES ($1, $2, $3, $4, $5, $6)
        ''', chat_id, mode, context_topic, question, answer,
            json.dumps(mcp_sources or []))


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


async def get_qa_count(chat_id: int) -> int:
    """Получить количество заданных вопросов"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT COUNT(*) as count FROM qa_history WHERE chat_id = $1',
            chat_id
        )
        return row['count']
