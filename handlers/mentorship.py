"""
Рабочее место наставника — команды регистрации потока и согласия (WP-578 Ф2).

/mentor_stream <STREAM_ID> — группа: наставник/пилот регистрирует эту группу
    за потоком (S1/S2/…). Идемпотентно, аудируемо (stream_chat).
/mentor_consent — личка: участник даёт или отзывает согласие на сохранение
    переписки (оба scope сразу, одной кнопкой — Р7, DRR-f2 §5).

Известное сужение MVP: deep-link из дисклеймера группы (t.me/<bot>?start=…)
не реализован в этом проходе — `/start` уже занят онбордингом
(handlers/onboarding.py), которого мы намеренно не трогаем в объёме Ф2.
Дисклеймер (Ф2, следующий шаг) вместо ссылки указывает участнику команду
`/mentor_consent` напрямую.

Разбор решения: DS-my-strategy/inbox/WP-578/DRR-f2-telegram-bridge.md
"""

from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db.queries.consent import set_consent_grant
from db.queries.mentorship import register_stream_chat
from helpers.dual_write import resolve_ory_id_from_chat

logger = logging.getLogger(__name__)

mentorship_router = Router(name="mentorship")

_CONSENT_SCOPES = ("mentor_archive_dm", "mentor_archive_group")

_REGISTER_RESULT_TEXT = {
    "registered": "✅ Группа зарегистрирована за потоком {stream}.",
    "already_registered": "Группа уже зарегистрирована за потоком {stream}.",
    "not_stream_reader": "Не удалось: вы не числитесь наставником или пилотом потока {stream}.",
}


@mentorship_router.message(Command("mentor_stream"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_mentor_stream(message: Message, command: CommandObject) -> None:
    stream_id = (command.args or "").strip().upper()
    if not stream_id:
        await message.reply("Укажи поток: /mentor_stream S1")
        return

    account_id = await resolve_ory_id_from_chat(message.from_user.id)
    if account_id is None:
        await message.reply("Не нашёл твой аккаунт платформы — сначала привяжи его (/link).")
        return

    try:
        result = await register_stream_chat(message.chat.id, stream_id, account_id)
    except RuntimeError:
        logger.warning("[Mentorship] /mentor_stream вызван при отключённом модуле (MENTORSHIP_URL не задан)")
        await message.reply("Рабочее место наставника сейчас недоступно — обратись к пилоту.")
        return
    text = _REGISTER_RESULT_TEXT.get(result, "Не удалось зарегистрировать группу.")
    await message.reply(text.format(stream=stream_id))


def _consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, согласен", callback_data="mentor_consent:accept"),
                InlineKeyboardButton(text="Отозвать", callback_data="mentor_consent:revoke"),
            ]
        ]
    )


@mentorship_router.message(Command("mentor_consent"), F.chat.type == "private")
async def cmd_mentor_consent(message: Message) -> None:
    await message.answer(
        "Наставник вашего потока сохраняет переписку (группу и личные сообщения боту), "
        "чтобы быстро отвечать с учётом истории. Согласны?",
        reply_markup=_consent_keyboard(),
    )


@mentorship_router.callback_query(F.data.in_({"mentor_consent:accept", "mentor_consent:revoke"}))
async def cb_mentor_consent(callback: CallbackQuery) -> None:
    account_id = await resolve_ory_id_from_chat(callback.from_user.id)
    if account_id is None:
        await callback.answer("Не нашёл твой аккаунт платформы.", show_alert=True)
        return

    grant = callback.data == "mentor_consent:accept"
    for scope in _CONSENT_SCOPES:
        await set_consent_grant(account_id, scope, granted=grant)

    text = "✅ Согласие зафиксировано." if grant else "Согласие отозвано — новая переписка сохраняться не будет."
    await callback.message.edit_text(text)
    await callback.answer()
    logger.info("[Mentorship] consent %s account=%s", "granted" if grant else "revoked", account_id)
