from __future__ import annotations

"""
Запросы для истории вопросов и ответов.
"""

import asyncio
import json
from datetime import datetime
from typing import List, Optional

from config import get_logger
from db.connection import get_journal_pool
from helpers.dual_write import post_event, resolve_ory_id_from_chat

logger = get_logger(__name__)


async def save_qa(chat_id: int, mode: str, context_topic: str,
                  question: str, answer: str, mcp_sources: List[str] = None) -> Optional[int]:
    """Сохранить вопрос и ответ. Возвращает id записи."""
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            INSERT INTO public.qa_history
            (chat_id, mode, context_topic, question, answer, mcp_sources)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        ''', chat_id, mode, context_topic, question, answer,
            json.dumps(mcp_sources or []))
        qa_id = row['id'] if row else None

    # WP-268 Phase 2 Phase B: dual-write события qa_query.v1.
    # PII БЕЗОПАСНОСТЬ: payload содержит ТОЛЬКО metadata — длины строк, mode,
    # context_topic, sources count. Сырые question/answer НЕ передаются
    # в gateway (потенциальная PII: пользователь может задать вопрос с email,
    # ФИО, телефоном). Источник истины для содержимого Q&A остаётся в legacy
    # qa_history; gateway получает только сигнал «было обращение».
    if qa_id is not None:
        try:
            ory = await resolve_ory_id_from_chat(chat_id)
            asyncio.create_task(post_event(
                source="aist-bot",
                external_id=f"qa-{qa_id}",
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
            ))
        except Exception as exc:
            logger.warning(f"[QA] dual-write task schedule failed: {exc}")

    return qa_id


async def get_qa_history(chat_id: int, limit: int = 50) -> List[dict]:
    """Получить историю вопросов и ответов"""
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT * FROM public.qa_history
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
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM public.qa_history WHERE id = $1', qa_id
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
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT id FROM public.qa_history WHERE chat_id = $1 ORDER BY created_at DESC LIMIT 1',
            chat_id
        )
        return row['id'] if row else None


async def update_qa_helpful(qa_id: int, helpful: bool):
    """Записать feedback (helpful/not helpful)."""
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE public.qa_history SET helpful = $1 WHERE id = $2',
            helpful, qa_id
        )

    # WP-268 Phase 2 Phase B: dual-write feedback события.
    # external_id привязан к qa_id+helpful (идемпотентен при ретрае).
    try:
        chat_id = await _get_chat_id_for_qa(qa_id)
        ory = await resolve_ory_id_from_chat(chat_id) if chat_id else None
        asyncio.create_task(post_event(
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
        ))
    except Exception as exc:
        logger.warning(f"[QA] feedback dual-write schedule failed: {exc}")


async def update_qa_comment(qa_id: int, comment: str):
    """Записать замечание пользователя."""
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            'UPDATE public.qa_history SET user_comment = $1 WHERE id = $2',
            comment, qa_id
        )

    # WP-268 Phase 2 Phase B: dual-write события user_comment.
    # PII: сам текст comment'а НЕ передаётся (пользователь может вписать
    # личные данные). Только длина и факт обращения.
    try:
        chat_id = await _get_chat_id_for_qa(qa_id)
        ory = await resolve_ory_id_from_chat(chat_id) if chat_id else None
        asyncio.create_task(post_event(
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
        ))
    except Exception as exc:
        logger.warning(f"[QA] comment dual-write schedule failed: {exc}")


async def _get_chat_id_for_qa(qa_id: int) -> Optional[int]:
    """Внутренний helper для dual-write: вытаскивает chat_id из qa_history.

    Используется в update_qa_helpful/update_qa_comment чтобы получить ory_id
    через resolve_ory_id_from_chat. Best-effort: на ошибке возвращает None,
    dual-write пройдёт с account_id=None (gateway допускает).
    """
    try:
        pool = await get_journal_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT chat_id FROM public.qa_history WHERE id = $1', qa_id
            )
            return row['chat_id'] if row else None
    except Exception:
        return None


async def get_qa_count(chat_id: int) -> int:
    """Получить количество заданных вопросов"""
    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT COUNT(*) as count FROM public.qa_history WHERE chat_id = $1',
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

    pool = await get_journal_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE helpful = TRUE) AS helpful,
                COUNT(*) FILTER (WHERE helpful = FALSE) AS not_helpful,
                COUNT(*) FILTER (WHERE created_at >= $2) AS this_week
            FROM public.qa_history
            WHERE chat_id = $1
        ''', chat_id, week_start)

        topics = await conn.fetch('''
            SELECT context_topic AS topic, COUNT(*) AS cnt
            FROM public.qa_history
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
