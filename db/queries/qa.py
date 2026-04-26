"""
Запросы для истории вопросов и ответов.
"""

import json
from datetime import datetime
from typing import List, Optional

from config import get_logger
from db.connection import get_pool
from helpers.dual_write import post_event, resolve_ory_id_from_chat

logger = get_logger(__name__)


async def save_qa(chat_id: int, mode: str, context_topic: str,
                  question: str, answer: str, mcp_sources: List[str] = None) -> Optional[int]:
    """Сохранить вопрос и ответ — single-write на event-gateway (WP-268 cut-over).

    WP-268 cut-over: legacy INSERT INTO qa_history УДАЛЁН.
    Возвращаем синтетический id (hash от chat_id + content) — caller'ы используют
    его как ссылку для feedback (update_qa_helpful/comment).

    PII-инвариант: ни question, ни answer не передаются в gateway (только длины).

    ⚠️ Caller'ы, которые ЧИТАЮТ qa_history (get_qa_history, get_qa_by_id) —
    не работают для новых записей. Эти reader'ы должны мигрировать на новую БД
    в WP-269. До миграции (а) get_qa_history вернёт пустой список для cut-over
    периода, (б) get_qa_by_id вернёт None для синтетических id.
    """
    import hashlib as _hashlib

    # Синтетический qa_id — hash от content для идемпотентности retry.
    # Длина 8 hex chars = ~4 млрд значений, коллизии маловероятны для bot scale.
    content_hash = _hashlib.sha1(
        f"{chat_id}:{mode}:{question or ''}:{answer or ''}".encode()
    ).hexdigest()[:12]
    # int(...) для совместимости со старым тип-контрактом (int qa_id).
    qa_id = int(content_hash, 16) % (2**31)

    ory = await resolve_ory_id_from_chat(chat_id)
    await post_event(
        source="aist-bot",
        external_id=f"qa-{content_hash}",
        event_type="qa_query",
        schema_version="v1",
        occurred_at=datetime.utcnow(),
        account_id=ory,
        payload={
            "qa_id": qa_id,
            "mode": mode,
            "context_topic": context_topic or "",
            "question_length": len(question or ""),
            "answer_length": len(answer or ""),
            "mcp_sources_count": len(mcp_sources or []),
        },
    )

    return qa_id


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


async def update_qa_helpful(qa_id: int, helpful: bool, chat_id: Optional[int] = None):
    """Feedback (helpful) — single-write на event-gateway (WP-268 cut-over).

    WP-268 cut-over: legacy UPDATE qa_history УДАЛЁН.

    ⚠️ Сигнатура расширена: chat_id теперь параметр (раньше резолвили через
    _get_chat_id_for_qa, который читал legacy qa_history). Caller'ы должны
    передавать chat_id явно. Если None — account_id=None в envelope.

    Идемпотентность через external_id (qa_id+helpful flag).
    """
    ory = await resolve_ory_id_from_chat(chat_id) if chat_id else None
    await post_event(
        source="aist-bot",
        external_id=f"qa-feedback-{qa_id}-{int(helpful)}",
        event_type="qa_feedback",
        schema_version="v1",
        occurred_at=datetime.utcnow(),
        account_id=ory,
        payload={
            "qa_id": qa_id,
            "helpful": helpful,
        },
    )


async def update_qa_comment(qa_id: int, comment: str, chat_id: Optional[int] = None):
    """Comment feedback — single-write на event-gateway (WP-268 cut-over).

    WP-268 cut-over: legacy UPDATE qa_history.user_comment УДАЛЁН.

    ⚠️ Сигнатура расширена: chat_id теперь параметр (см. update_qa_helpful).
    PII: сам текст comment'а НЕ передаётся (только длина).
    """
    ory = await resolve_ory_id_from_chat(chat_id) if chat_id else None
    await post_event(
        source="aist-bot",
        external_id=f"qa-comment-{qa_id}",
        event_type="qa_comment",
        schema_version="v1",
        occurred_at=datetime.utcnow(),
        account_id=ory,
        payload={
            "qa_id": qa_id,
            "comment_length": len(comment or ""),
        },
    )


# WP-268 cut-over: _get_chat_id_for_qa(qa_id) удалён. После cut-over qa_history
# legacy не пишется → SELECT по qa_id вернёт None или старые записи. Caller'ы
# update_qa_helpful/update_qa_comment теперь принимают chat_id явно.


async def get_qa_count(chat_id: int) -> int:
    """Получить количество заданных вопросов"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT COUNT(*) as count FROM qa_history WHERE chat_id = $1',
            chat_id
        )
        return row['count']


async def get_user_qa_stats(chat_id: int) -> dict:
    """Статистика консультаций для одного пользователя.

    Returns:
        {total, helpful, not_helpful, this_week, top_topics: [{topic, cnt}]}
    """
    from datetime import timedelta
    from db.queries.users import moscow_today

    today = moscow_today()
    week_start = today - timedelta(days=today.weekday())

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE helpful = TRUE) AS helpful,
                COUNT(*) FILTER (WHERE helpful = FALSE) AS not_helpful,
                COUNT(*) FILTER (WHERE created_at >= $2) AS this_week
            FROM qa_history
            WHERE chat_id = $1
        ''', chat_id, week_start)

        topics = await conn.fetch('''
            SELECT context_topic AS topic, COUNT(*) AS cnt
            FROM qa_history
            WHERE chat_id = $1
              AND context_topic IS NOT NULL AND context_topic != ''
            GROUP BY context_topic
            ORDER BY cnt DESC
            LIMIT 5
        ''', chat_id)

    return {
        'total': row['total'] if row else 0,
        'helpful': row['helpful'] if row else 0,
        'not_helpful': row['not_helpful'] if row else 0,
        'this_week': row['this_week'] if row else 0,
        'top_topics': [dict(t) for t in topics],
    }
