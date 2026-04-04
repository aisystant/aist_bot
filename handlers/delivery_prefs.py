"""
Хендлеры предпочтений стиля подачи (WP-151 Ф2).

Два момента показа:
1. После первого завершённого урока (флаг delivery_prefs_pending в current_context)
2. Через /update → upd_delivery (настройки профиля)

Два вопроса (inline-кнопки, без FSM):
- Формат подачи: examples / tasks / mix
- Детализация: brief / detailed
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


# ============= ПОКАЗ ВОПРОСОВ (вызывается из mode_select или settings) =============

async def show_delivery_format_question(message: Message, lang: str = 'ru'):
    """Показать первый вопрос: формат подачи."""
    await message.answer(
        t('delivery.ask_format', lang),
        reply_markup=kb_delivery_format(lang),
    )


# ============= CALLBACK: ФОРМАТ ПОДАЧИ =============

@delivery_prefs_router.callback_query(F.data.startswith("delf_"))
async def on_delivery_format(callback: CallbackQuery, state: FSMContext):
    """Сохранить формат подачи и показать второй вопрос."""
    chat_id = callback.message.chat.id
    format_value = callback.data.replace("delf_", "")

    await update_intern(chat_id, delivery_format=format_value)
    await callback.answer()

    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru'

    # Если detail_level уже задан (повторное изменение через настройки) — просто подтвердить
    if intern.get('detail_level'):
        await callback.message.edit_text(
            t('delivery.saved', lang),
        )
        return

    # Показать второй вопрос
    await callback.message.edit_text(
        t('delivery.ask_detail', lang),
        reply_markup=kb_detail_level(lang),
    )


# ============= CALLBACK: ДЕТАЛИЗАЦИЯ =============

@delivery_prefs_router.callback_query(F.data.startswith("detl_"))
async def on_detail_level(callback: CallbackQuery, state: FSMContext):
    """Сохранить детализацию и завершить."""
    chat_id = callback.message.chat.id
    detail_value = callback.data.replace("detl_", "")

    await update_intern(chat_id, detail_level=detail_value)
    await callback.answer()

    intern = await get_intern(chat_id)
    lang = intern.get('language', 'ru') or 'ru'

    # Убрать флаг pending (если был)
    ctx = intern.get('current_context', {}) or {}
    if ctx.get('delivery_prefs_pending'):
        ctx.pop('delivery_prefs_pending')
        await update_intern(chat_id, current_context=ctx)

    await callback.message.edit_text(
        t('delivery.saved', lang),
    )
    logger.info(f"[DeliveryPrefs] User {chat_id}: format={intern.get('delivery_format')}, detail={detail_value}")


# ============= SETTINGS: /update → upd_delivery =============

@delivery_prefs_router.callback_query(F.data == "upd_delivery")
async def on_upd_delivery(callback: CallbackQuery, state: FSMContext):
    """Показать выбор стиля подачи из настроек профиля."""
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru'
    current_format = intern.get('delivery_format') or '-'
    current_detail = intern.get('detail_level') or '-'

    await callback.answer()
    await callback.message.edit_text(
        f"{t('delivery.current', lang)}\n"
        f"{t('delivery.current_format', lang)}: *{current_format}*\n"
        f"{t('delivery.current_detail', lang)}: *{current_detail}*\n\n"
        f"{t('delivery.ask_format', lang)}",
        parse_mode="Markdown",
        reply_markup=kb_delivery_format(lang),
    )
