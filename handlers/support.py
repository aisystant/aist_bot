"""
/support — хендлер службы поддержки.

Сценарий (DP.SC.150 §1):
  /support → выбор темы → описание → приоритет
           → создание тикета в Chatwoot → подтверждение пользователю
"""

import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db.queries import get_intern
from db.queries.users import is_onboarded

logger = logging.getLogger(__name__)

support_router = Router(name="support")

_TOPICS = {
    "bug":   "🐛 Баг",
    "quest": "❓ Вопрос",
    "wish":  "💡 Пожелание",
    "other": "📋 Прочее",
}

_PRIORITY = {
    "urgent": "🚨 Срочно",
    "normal": "🔵 Обычный",
}


class SupportState(StatesGroup):
    topic = State()
    description = State()
    priority = State()


def _topic_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🐛 Баг", callback_data="sup_topic:bug"),
            InlineKeyboardButton(text="❓ Вопрос", callback_data="sup_topic:quest"),
        ],
        [
            InlineKeyboardButton(text="💡 Пожелание", callback_data="sup_topic:wish"),
            InlineKeyboardButton(text="📋 Прочее", callback_data="sup_topic:other"),
        ],
    ])


def _priority_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚨 Срочно", callback_data="sup_pri:urgent"),
        InlineKeyboardButton(text="🔵 Обычный", callback_data="sup_pri:normal"),
    ]])


@support_router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext):
    intern = await get_intern(message.chat.id)
    if not await is_onboarded(intern):
        await message.answer("Сначала пройдите /start")
        return
    await state.clear()
    await state.set_state(SupportState.topic)
    await message.answer(
        "🛟 *Служба поддержки*\n\nВыберите тему обращения:",
        parse_mode="Markdown",
        reply_markup=_topic_kb(),
    )


@support_router.callback_query(StateFilter(SupportState.topic), F.data.startswith("sup_topic:"))
async def cb_topic(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":")[1]
    label = _TOPICS.get(key, key)
    await state.update_data(topic=key, topic_label=label)
    await state.set_state(SupportState.description)
    await callback.message.edit_text(
        f"Тема: *{label}*\n\nОпишите проблему или вопрос:",
        parse_mode="Markdown",
    )
    await callback.answer()


@support_router.message(StateFilter(SupportState.description))
async def msg_description(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 5:
        await message.answer("Пожалуйста, опишите подробнее (минимум 5 символов).")
        return
    await state.update_data(description=message.text.strip())
    await state.set_state(SupportState.priority)
    await message.answer("Приоритет:", reply_markup=_priority_kb())


@support_router.callback_query(StateFilter(SupportState.priority), F.data.startswith("sup_pri:"))
async def cb_priority(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":")[1]
    label = _PRIORITY.get(key, key)
    data = await state.get_data()
    await state.clear()

    topic_label = data.get("topic_label", data.get("topic", ""))
    description = data.get("description", "")
    chat_id = callback.message.chat.id
    user = callback.from_user
    name = user.full_name or f"tg_{chat_id}"

    await callback.message.edit_text("⏳ Создаю тикет…")
    await callback.answer()

    try:
        from clients.chatwoot import get_or_create_contact, create_conversation, send_message, CHATWOOT_URL
        from db.queries.helpdesk import create_ticket, update_ticket_conversation

        if not CHATWOOT_URL:
            await callback.message.edit_text(
                "⚠️ Служба поддержки временно недоступна. Напишите @aisystant напрямую."
            )
            return

        ticket_id = await create_ticket(chat_id, data.get("topic", ""))

        contact = await get_or_create_contact(chat_id, name)
        if not contact:
            await callback.message.edit_text("⚠️ Не удалось создать контакт. Попробуйте позже.")
            return

        source_id = contact.get("source_id")
        contact_identifier = contact.get("identifier") or str(contact.get("id", ""))
        if not source_id:
            await callback.message.edit_text("⚠️ Не удалось создать контакт. Попробуйте позже.")
            return

        conv = await create_conversation(source_id)
        if not conv:
            await callback.message.edit_text("⚠️ Не удалось открыть тикет. Попробуйте позже.")
            return

        conv_id = conv.get("id")
        await update_ticket_conversation(ticket_id, conv_id, contact_identifier)

        initial_msg = (
            f"Тема: {topic_label}\n"
            f"Приоритет: {label}\n\n"
            f"{description}"
        )
        await send_message(source_id, conv_id, initial_msg)

        await callback.message.edit_text(
            f"✅ *Тикет #{conv_id} создан*\n\n"
            f"Тема: {topic_label} · {label}\n\n"
            f"Агент ответит в этом чате. Обычно в течение 24 часов.",
            parse_mode="Markdown",
        )
        logger.info("[support] ticket created: chat_id=%s conv_id=%s topic=%s", chat_id, conv_id, data.get("topic"))

    except Exception as e:
        logger.error("[support] error creating ticket for chat_id=%s: %s", chat_id, e)
        await callback.message.edit_text("⚠️ Ошибка при создании тикета. Попробуйте позже.")
