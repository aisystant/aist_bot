"""
Хендлеры предпочтений стиля подачи (WP-151 Ф2).

Callback-обработка delf_*/detl_* происходит в SM states:
- states/common/profile.py — из настроек профиля
- states/common/mode_select.py — после первого урока (pending)

Этот модуль содержит:
- delivery_prefs_router — только для legacy settings (UpdateStates)
- Вспомогательные функции (show, save) для вызова из SM
"""

import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from db.queries import get_intern, update_intern
from i18n import t
from integrations.telegram.keyboards import kb_delivery_format, kb_detail_level

logger = logging.getLogger(__name__)

delivery_prefs_router = Router(name="delivery_prefs")


# ============= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (вызываются из SM states) =============

async def save_delivery_format(chat_id: int, format_value: str):
    """Сохранить формат подачи в БД."""
    await update_intern(chat_id, delivery_format=format_value)
    logger.info(f"[DeliveryPrefs] User {chat_id}: format={format_value}")


async def save_detail_level(chat_id: int, detail_value: str):
    """Сохранить детализацию в БД и убрать флаг pending."""
    await update_intern(chat_id, detail_level=detail_value)

    intern = await get_intern(chat_id)
    ctx = intern.get('current_context', {}) or {}
    if ctx.get('delivery_prefs_pending'):
        ctx.pop('delivery_prefs_pending')
        await update_intern(chat_id, current_context=ctx)

    logger.info(f"[DeliveryPrefs] User {chat_id}: detail={detail_value}")
