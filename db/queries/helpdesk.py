"""
DB queries для helpdesk_tickets (WP-341).
"""

import logging
from datetime import datetime

from db.connection import get_pool

logger = logging.getLogger(__name__)


async def create_ticket(chat_id: int, topic: str) -> int:
    """Insert a new ticket, return its id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO public.helpdesk_tickets (chat_id, topic, created_at, updated_at)
            VALUES ($1, $2, $3, $3)
            RETURNING id
            """,
            chat_id, topic, datetime.utcnow(),
        )


async def update_ticket_conversation(ticket_id: int, conversation_id: int, contact_identifier: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE public.helpdesk_tickets
            SET conversation_id = $1, contact_identifier = $2, updated_at = $3
            WHERE id = $4
            """,
            conversation_id, contact_identifier, datetime.utcnow(), ticket_id,
        )


async def get_chat_id_by_conversation(conversation_id: int) -> int | None:
    """Look up Telegram chat_id by Chatwoot conversation_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT chat_id FROM public.helpdesk_tickets WHERE conversation_id = $1 LIMIT 1",
            conversation_id,
        )


async def close_ticket(conversation_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.helpdesk_tickets SET status = 'closed', updated_at = $1 WHERE conversation_id = $2",
            datetime.utcnow(), conversation_id,
        )
