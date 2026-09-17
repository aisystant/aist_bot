"""
Рабочее место наставника — команды регистрации потока, согласия и заметок
(WP-578 Ф2/Ф3).

/mentor_stream <STREAM_ID> — группа: наставник/пилот регистрирует эту группу
    за потоком (S1/S2/…). Идемпотентно, аудируемо (stream_chat).
/mentor_consent — личка: участник даёт или отзывает согласие на сохранение
    переписки (оба scope сразу, одной кнопкой — Р7, DRR-f2 §5).
/mentor_invite — группа, ответом на сообщение участника: наставник/пилот
    просит бота лично написать этому участнику запрос согласия в личку
    (та же кнопка, что у /mentor_consent) — WP-578, обнаружение согласия,
    способ 2 из 3 (решение пилота 17.09, способ 3 — встроить в онбординг —
    не делаем в этом проходе).
/mentor_note [текст] — группа, ответом на сообщение участника: сохранить
    сообщение-цель (или явный аргумент команды) как заметку наставника через
    mentorship-service (WP-578 Ф3, add_participant_note). Бот НЕ пишет в
    базу наставничества напрямую (принцип «бот = тонкий клиент», MEMORY.md) —
    только HTTP-вызов в отдельный сервис после подтверждения карантина.

Известное сужение MVP: deep-link из дисклеймера группы (t.me/<bot>?start=…)
не реализован в этом проходе — `/start` уже занят онбордингом
(handlers/onboarding.py), которого мы намеренно не трогаем в объёме Ф2.
Периодическое сообщение в группе (способ 1 из 3) — core/scheduler.py.

Разбор решения: DS-my-strategy/inbox/WP-578/DRR-f2-telegram-bridge.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import time

from aiogram import Router, F
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clients.mentorship_service import MentorshipServiceError, mentorship_service
from db.queries.consent import set_consent_grant
from db.queries.mentorship import get_stream_reader_role, lookup_stream_chat, register_stream_chat
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


# Общий текст запроса согласия — три поверхности показа: сама команда
# (cmd_mentor_consent), личное приглашение по инициативе наставника
# (cmd_mentor_invite) и периодическое сообщение в группе
# (core/scheduler.py:_send_mentorship_disclaimer). Одна формулировка, не три
# рассинхронизирующихся копии (P2).
CONSENT_PROMPT_TEXT = (
    "Наставник вашего потока сохраняет переписку (группу и личные сообщения боту), "
    "чтобы быстро отвечать с учётом истории. Согласны?"
)


def consent_keyboard() -> InlineKeyboardMarkup:
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
    await message.answer(CONSENT_PROMPT_TEXT, reply_markup=consent_keyboard())


@mentorship_router.message(Command("mentor_invite"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_mentor_invite(message: Message) -> None:
    """Наставник отвечает командой на сообщение участника — бот лично пишет
    этому участнику запрос согласия в личку (обнаружение согласия, способ 2
    из 3, решение пилота 17.09). Участника, ещё ни разу не писавшего боту,
    Телеграм не даёт боту заговорить первым — это ожидаемый, не ошибочный,
    исход (см. TelegramForbiddenError ниже)."""
    target_message = message.reply_to_message
    if target_message is None or target_message.from_user is None:
        await message.reply("Ответь этой командой на сообщение участника, которому шлём приглашение.")
        return
    if target_message.from_user.id == message.from_user.id:
        await message.reply("Нельзя пригласить самого себя.")
        return

    caller_account_id = await resolve_ory_id_from_chat(message.from_user.id)
    if caller_account_id is None:
        await message.reply("Не нашёл твой аккаунт платформы — сначала привяжи его (/link).")
        return

    ctx = await lookup_stream_chat(message.chat.id)
    if ctx is None:
        await message.reply("Эта группа не зарегистрирована за потоком — сначала /mentor_stream.")
        return
    if await get_stream_reader_role(caller_account_id, ctx.stream_id) is None:
        await message.reply(f"Ты не числишься наставником или пилотом потока {ctx.stream_id}.")
        return

    target_account_id = await resolve_ory_id_from_chat(target_message.from_user.id)
    if target_account_id is None:
        await message.reply("У участника нет привязанного аккаунта платформы — приглашение недоступно.")
        return

    target_name = target_message.from_user.full_name
    try:
        await message.bot.send_message(
            target_message.from_user.id,
            CONSENT_PROMPT_TEXT,
            reply_markup=consent_keyboard(),
        )
    except TelegramForbiddenError:
        logger.info(
            "[Mentorship] /mentor_invite: бот не может написать первым account=%s (участник ещё не открывал чат с ботом)",
            target_account_id,
        )
        await message.reply(
            f"Не получилось написать {target_name} — участник ещё ни разу не писал боту в личку, "
            "Телеграм не разрешает боту заговорить первым. Попроси его прислать боту любое сообщение, "
            "потом повтори приглашение."
        )
        return

    logger.info("[Mentorship] /mentor_invite отправлено by=%s to=%s", caller_account_id, target_account_id)
    await message.reply(f"Приглашение отправлено участнику {target_name} в личку.")


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


@dataclass(frozen=True)
class _PendingNote:
    mentor_account_id: str
    stream_id: str
    participant_account_id: str
    participant_name: str
    body: str
    created_at: float


_PENDING_NOTE_TTL_SECONDS = 15 * 60

# (chat_id, mentor_telegram_user_id) -> заметка, ждущая подтверждения карантина
# кнопкой. In-memory, не переживает рестарт бота (Railway redeploy) — то же
# осознанное узкое сужение, что _topic_creator_is_mentor_cache в
# engines/mentorship/archive_tap.py: потеря означает "наставник подтверждает
# ещё раз", не порчу данных (CLAUDE.md §10.36, исключение для UI-флагов без
# побочных эффектов). Новый /mentor_note от того же наставника в том же чате
# молча заменяет предыдущий незавершённый — одна незавершённая заметка на
# наставника в чате достаточно для MVP.
_pending_notes: dict[tuple[int, int], _PendingNote] = {}


def _note_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, сохранить", callback_data="mentor_note:confirm"),
                InlineKeyboardButton(text="Отмена", callback_data="mentor_note:cancel"),
            ]
        ]
    )


@mentorship_router.message(Command("mentor_note"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_mentor_note(message: Message, command: CommandObject) -> None:
    """Наставник отвечает командой на сообщение участника — бот запоминает
    текст (аргумент команды, если он есть, иначе текст сообщения-цели) и
    просит подтвердить отсутствие чужих личных данных, прежде чем звать
    add_participant_note сервиса (тот сам fail-closed без этого подтверждения,
    WP-578 Ф3 — двойная защита намеренная, не дублирование)."""
    target_message = message.reply_to_message
    if target_message is None or target_message.from_user is None:
        await message.reply("Ответь этой командой на сообщение участника, которое нужно сохранить как заметку.")
        return
    if target_message.from_user.id == message.from_user.id:
        await message.reply("Нельзя сохранить собственное сообщение наставника как заметку об участнике.")
        return

    body = (command.args or "").strip() or (target_message.text or target_message.caption or "").strip()
    if not body:
        await message.reply("В сообщении-цели нет текста — нечего сохранять.")
        return

    caller_account_id = await resolve_ory_id_from_chat(message.from_user.id)
    if caller_account_id is None:
        await message.reply("Не нашёл твой аккаунт платформы — сначала привяжи его (/link).")
        return

    ctx = await lookup_stream_chat(message.chat.id)
    if ctx is None:
        await message.reply("Эта группа не зарегистрирована за потоком — сначала /mentor_stream.")
        return
    if await get_stream_reader_role(caller_account_id, ctx.stream_id) is None:
        await message.reply(f"Ты не числишься наставником или пилотом потока {ctx.stream_id}.")
        return

    participant_account_id = await resolve_ory_id_from_chat(target_message.from_user.id)
    if participant_account_id is None:
        await message.reply("У участника нет привязанного аккаунта платформы — заметку сохранить нельзя.")
        return

    participant_name = target_message.from_user.full_name
    _pending_notes[(message.chat.id, message.from_user.id)] = _PendingNote(
        mentor_account_id=caller_account_id,
        stream_id=ctx.stream_id,
        participant_account_id=participant_account_id,
        participant_name=participant_name,
        body=body,
        created_at=time(),
    )
    await message.reply(
        f"Сохранить как заметку об участнике {participant_name}?\n\n«{body}»\n\n"
        "⚠️ Подтверди, что в тексте нет чужих личных данных без согласия.",
        reply_markup=_note_confirm_keyboard(),
    )


@mentorship_router.callback_query(F.data.in_({"mentor_note:confirm", "mentor_note:cancel"}))
async def cb_mentor_note(callback: CallbackQuery) -> None:
    key = (callback.message.chat.id, callback.from_user.id)
    pending = _pending_notes.pop(key, None)
    if pending is None:
        await callback.answer("Заметка устарела или уже обработана — повтори /mentor_note.", show_alert=True)
        return

    if callback.data == "mentor_note:cancel":
        await callback.message.edit_text("Отменено — заметка не сохранена.")
        await callback.answer()
        return

    if time() - pending.created_at > _PENDING_NOTE_TTL_SECONDS:
        await callback.answer("Заметка устарела — повтори /mentor_note.", show_alert=True)
        await callback.message.edit_text("Заметка устарела — повтори /mentor_note.")
        return

    try:
        result = await mentorship_service.add_participant_note(
            pending.mentor_account_id,
            pending.stream_id,
            pending.participant_account_id,
            pending.body,
            source_hint="telegram-forward",
            quarantine_confirmed=True,
        )
    except MentorshipServiceError as e:
        logger.warning("[Mentorship] add_participant_note отклонён: %s — %s", e.kind, e)
        await callback.message.edit_text(f"Не удалось сохранить заметку: {e}")
        await callback.answer()
        return

    if result is None:
        await callback.message.edit_text("Сервис заметок сейчас недоступен — попробуй позже.")
        await callback.answer()
        return

    logger.info(
        "[Mentorship] add_participant_note by=%s participant=%s stream=%s",
        pending.mentor_account_id,
        pending.participant_account_id,
        pending.stream_id,
    )
    await callback.message.edit_text(f"✅ Заметка об участнике {pending.participant_name} сохранена.")
    await callback.answer()
