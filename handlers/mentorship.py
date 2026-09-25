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
/mentor_note [текст] — личка, ответом на сообщение, пересланное туда же
    наставником: тот же поток подтверждения, что и в группе, но участник
    определяется без группового контекста (WP-578, актуализация 19.09,
    личные 1:1 переписки наставника с участником вне бота). Если пересылается
    сообщение, которое написал сам участник — Телеграм называет автора
    однозначно, бот резолвит его напрямую. Если пересылается собственное
    сообщение наставника (кому оно было отправлено, Телеграм не хранит) —
    бот использует участника из последнего однозначно определённого forward
    этой же личной сессии («активный участник», в памяти процесса).
/mentor_card — группа, ответом на сообщение участника: показать карточку
    участника (get_participant_card mentorship-service) — переписка, заметки
    наставника, статус внешних источников. Карточка содержит приватные данные
    об участнике — уходит наставнику ЛИЧНЫМ сообщением, не в группу (в группу
    только короткое подтверждение, тот же принцип, что у F9 — usage-ошибки
    идут в личку, здесь ещё и сам результат). Бот = тонкий клиент: сервис уже
    полностью реализован (WP-578 Ф3), команда только вызывает и форматирует
    (WP-578, новое требование пилота 23.09).
/mentor_card — личка, ответом на пересланное сообщение: тот же способ
    определения участника, что у DM-версии /mentor_note; карточка уже в
    личном чате с ботом, лишней пересылки не нужно.

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
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from clients.mentorship_service import MentorshipServiceError, mentorship_service
from db.queries.consent import set_consent_grant
from db.queries.mentorship import (
    get_stream_reader_role,
    list_reader_streams,
    lookup_participant_stream,
    lookup_stream_chat,
    register_stream_chat,
)
from helpers.dual_write import resolve_ory_id_from_chat

logger = logging.getLogger(__name__)

mentorship_router = Router(name="mentorship")

_CONSENT_SCOPES = ("mentor_archive_dm", "mentor_archive_group")

_REGISTER_RESULT_TEXT = {
    "registered": "✅ Группа зарегистрирована за потоком {stream}.",
    "already_registered": "Группа уже зарегистрирована за потоком {stream}.",
    "not_stream_reader": "Не удалось: вы не числитесь наставником или пилотом потока {stream}.",
}


@dataclass(frozen=True)
class _Reader:
    account_id: str
    streams: dict[str, str]  # stream_id -> 'mentor' | 'pilot'


async def _resolve_reader(message: Message) -> _Reader | None:
    """Who called a group mentorship command: a stream reader or an outsider.

    None means stay silent: an outsider gets neither a group reply nor a DM
    (WP-578 F9). Causes look the same from outside but differ in the log; without
    the warnings a healthy guard, a disabled module and a failing database would be
    indistinguishable. Any lookup failure fails closed (silence), never open.
    """
    account_id = await resolve_ory_id_from_chat(message.from_user.id)
    if account_id is None:
        return None
    try:
        streams = await list_reader_streams(account_id)
    except RuntimeError:
        logger.warning("[Mentorship] проверка наставника пропущена: модуль отключён (MENTORSHIP_URL не задан)")
        return None
    except Exception as exc:
        logger.warning("[Mentorship] проверка наставника не удалась: %s", type(exc).__name__)
        return None
    if not streams:
        return None
    return _Reader(account_id=account_id, streams=dict(streams))


async def _tell_mentor(message: Message, text: str) -> bool:
    """Usage errors go to the mentor's DM, never to the group; a failed delivery is only logged.

    Returns whether the DM was actually delivered — most callers only fire
    usage-error text and ignore it (silence-on-failure is already the
    convention there), but a caller that follows up with a group-visible
    confirmation MUST check this, or the group sees a false "sent" for a DM
    that never arrived (WP-578, found in review 25.09)."""
    try:
        await message.bot.send_message(message.from_user.id, text)
        return True
    except TelegramAPIError as exc:
        # TelegramForbiddenError = the mentor never opened the bot's DM (no group fallback by design)
        logger.warning("[Mentorship] личное сообщение наставнику не доставлено: %s", type(exc).__name__)
        return False


async def _stream_of_group(message: Message, reader: _Reader) -> str | None:
    """Stream the group is registered to, if the caller reads exactly that stream.

    Otherwise None and the reason goes to the mentor's DM: reading another
    stream does not authorize acting in this group.
    """
    ctx = await lookup_stream_chat(message.chat.id)
    if ctx is None:
        await _tell_mentor(message, "Эта группа не зарегистрирована за потоком — сначала /mentor_stream.")
        return None
    if ctx.stream_id not in reader.streams:
        await _tell_mentor(message, f"Ты не числишься наставником или пилотом потока {ctx.stream_id}.")
        return None
    return ctx.stream_id


@mentorship_router.message(Command("mentor_stream"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_mentor_stream(message: Message, command: CommandObject) -> None:
    reader = await _resolve_reader(message)
    if reader is None:
        return

    own_streams = ", ".join(sorted(reader.streams))
    stream_id = (command.args or "").strip().upper()
    if not stream_id:
        await _tell_mentor(message, f"Укажи код потока: /mentor_stream <код>. Твои потоки: {own_streams}.")
        return
    if stream_id not in reader.streams:
        await _tell_mentor(
            message,
            f"Не удалось: вы не числитесь наставником или пилотом потока {stream_id}. Твои потоки: {own_streams}.",
        )
        return

    result = await register_stream_chat(message.chat.id, stream_id, reader.account_id)
    text = _REGISTER_RESULT_TEXT.get(result, "Не удалось зарегистрировать группу.").format(stream=stream_id)
    if result == "not_stream_reader":
        await _tell_mentor(message, text)
        return
    await message.reply(text)


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


# Отдельная область согласия для уже написанного раньше (WP-578 Ф11, импорт
# истории из экспорта Telegram Desktop). НЕ добавлена в _CONSENT_SCOPES/
# consent_keyboard() выше: то согласие смотрит в будущее (что собирать
# дальше), это — в прошлое (что уже написано), одной кнопкой их путать
# нельзя (peer-сессия 23.09, инвариант таблицы consent_grant — "Retroactive
# expansion запрещена", neon-migrations/mvp/229-wp316-consent-grant.sql:29).
_HISTORY_CONSENT_SCOPE = "mentor_archive_history"

HISTORY_CONSENT_PROMPT_TEXT = (
    "Наставник хочет перенести в архив и уже написанное вами раньше в общей "
    "группе потока и в личных сообщениях боту (не только новые сообщения — "
    "это отдельное решение). Согласны?"
)


def history_consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, согласен", callback_data="mentor_consent_history:accept"),
                InlineKeyboardButton(text="Отозвать", callback_data="mentor_consent_history:revoke"),
            ]
        ]
    )


@mentorship_router.message(Command("mentor_consent_history"), F.chat.type == "private")
async def cmd_mentor_consent_history(message: Message) -> None:
    await message.answer(HISTORY_CONSENT_PROMPT_TEXT, reply_markup=history_consent_keyboard())


@mentorship_router.callback_query(F.data.in_({"mentor_consent_history:accept", "mentor_consent_history:revoke"}))
async def cb_mentor_consent_history(callback: CallbackQuery) -> None:
    account_id = await resolve_ory_id_from_chat(callback.from_user.id)
    if account_id is None:
        await callback.answer("Не нашёл твой аккаунт платформы.", show_alert=True)
        return

    grant = callback.data == "mentor_consent_history:accept"
    await set_consent_grant(account_id, _HISTORY_CONSENT_SCOPE, granted=grant)

    text = "✅ Согласие на перенос прошлой переписки зафиксировано." if grant else "Согласие отозвано."
    await callback.message.edit_text(text)
    await callback.answer()
    logger.info("[Mentorship] history consent %s account=%s", "granted" if grant else "revoked", account_id)


@mentorship_router.message(Command("mentor_invite"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_mentor_invite(message: Message) -> None:
    """Наставник отвечает командой на сообщение участника — бот лично пишет
    этому участнику запрос согласия в личку (обнаружение согласия, способ 2
    из 3, решение пилота 17.09). Участника, ещё ни разу не писавшего боту,
    Телеграм не даёт боту заговорить первым — это ожидаемый, не ошибочный,
    исход (см. TelegramForbiddenError ниже)."""
    reader = await _resolve_reader(message)
    if reader is None:
        return

    target_message = message.reply_to_message
    if target_message is None or target_message.from_user is None:
        await _tell_mentor(message, "Ответь этой командой на сообщение участника, которому шлём приглашение.")
        return
    if target_message.from_user.id == message.from_user.id:
        await _tell_mentor(message, "Нельзя пригласить самого себя.")
        return

    if await _stream_of_group(message, reader) is None:
        return

    target_account_id = await resolve_ory_id_from_chat(target_message.from_user.id)
    if target_account_id is None:
        await _tell_mentor(message, "У участника нет привязанного аккаунта платформы — приглашение недоступно.")
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
        await _tell_mentor(
            message,
            f"Не получилось написать {target_name} — участник ещё ни разу не писал боту в личку, "
            "Телеграм не разрешает боту заговорить первым. Попроси его прислать боту любое сообщение, "
            "потом повтори приглашение.",
        )
        return

    logger.info("[Mentorship] /mentor_invite отправлено by=%s to=%s", reader.account_id, target_account_id)
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
    # Не редактируем callback.message: в группе кнопка общая для всех
    # участников потока (дисклеймер, DRR-f2 §5) — edit_text снял бы клавиатуру
    # для всех после первого клика, хотя согласие пишется per-account_id и
    # каждый должен иметь возможность кликнуть сам (найдено пир-сессией 24.09).
    await callback.answer(text, show_alert=True)
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


async def _stage_pending_note(
    message: Message,
    *,
    mentor_account_id: str,
    stream_id: str,
    participant_account_id: str,
    participant_name: str,
    body: str,
) -> None:
    """Общий хвост group- и DM-версий /mentor_note: запомнить заметку до
    подтверждения кнопкой и показать наставнику текст на проверку (карантин
    чужих личных данных — то же требование, что у сообщения-участника,
    WP-578 Ф3)."""
    _pending_notes[(message.chat.id, message.from_user.id)] = _PendingNote(
        mentor_account_id=mentor_account_id,
        stream_id=stream_id,
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


@mentorship_router.message(Command("mentor_note"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_mentor_note(message: Message, command: CommandObject) -> None:
    """Наставник отвечает командой на сообщение участника — бот запоминает
    текст (аргумент команды, если он есть, иначе текст сообщения-цели) и
    просит подтвердить отсутствие чужих личных данных, прежде чем звать
    add_participant_note сервиса (тот сам fail-closed без этого подтверждения,
    WP-578 Ф3 — двойная защита намеренная, не дублирование)."""
    reader = await _resolve_reader(message)
    if reader is None:
        return

    target_message = message.reply_to_message
    if target_message is None or target_message.from_user is None:
        await _tell_mentor(message, "Ответь этой командой на сообщение участника, которое нужно сохранить как заметку.")
        return
    if target_message.from_user.id == message.from_user.id:
        await _tell_mentor(message, "Нельзя сохранить собственное сообщение наставника как заметку об участнике.")
        return

    body = (command.args or "").strip() or (target_message.text or target_message.caption or "").strip()
    if not body:
        await _tell_mentor(message, "В сообщении-цели нет текста — нечего сохранять.")
        return

    stream_id = await _stream_of_group(message, reader)
    if stream_id is None:
        return

    participant_account_id = await resolve_ory_id_from_chat(target_message.from_user.id)
    if participant_account_id is None:
        await _tell_mentor(message, "У участника нет привязанного аккаунта платформы — заметку сохранить нельзя.")
        return

    await _stage_pending_note(
        message,
        mentor_account_id=reader.account_id,
        stream_id=stream_id,
        participant_account_id=participant_account_id,
        participant_name=target_message.from_user.full_name,
        body=body,
    )


@dataclass(frozen=True)
class _ActiveParticipant:
    stream_id: str
    participant_account_id: str
    participant_name: str
    set_at: float


_ACTIVE_PARTICIPANT_TTL_SECONDS = 2 * 60 * 60

# mentor_telegram_user_id -> участник, о котором шла речь в последнем
# однозначно определённом forward'е этой личной переписки с ботом. Нужен
# только для DM-версии /mentor_note: пересланное собственное сообщение
# наставника Телеграм не размечает адресатом (физически не хранит, кому оно
# было отправлено, WP-578 — обсуждение 17.09), поэтому бот переиспользует
# последнего участника, определённого однозначно (по forward_origin
# участника). In-memory, тот же осознанный компромисс, что _pending_notes
# выше — не переживает рестарт, наставник просто перешлёт сообщение
# участника ещё раз, данные не портятся.
_active_participant: dict[int, _ActiveParticipant] = {}


async def _resolve_dm_participant_target(
    message: Message, target_message: Message
) -> tuple[str | None, str | None, str | None, str | None]:
    """Кому адресован DM-запрос (заметка или карточка) из пересланного в
    личку сообщения — общий резолвер для /mentor_note и /mentor_card (WP-578).

    Возвращает (stream_id, participant_account_id, participant_name, error).
    error непустой ⇒ остальные три поля None, вызывающий просто показывает
    текст ошибки пилоту как есть."""
    origin = getattr(target_message, "forward_origin", None)
    origin_user = getattr(origin, "sender_user", None) if origin is not None else None

    if origin_user is not None and origin_user.id != message.from_user.id:
        # Переслано сообщение, которое написал сам участник — Телеграм
        # однозначно называет автора, угадывать не нужно.
        participant_account_id = await resolve_ory_id_from_chat(origin_user.id)
        if participant_account_id is None:
            return None, None, None, "У этого участника нет привязанного аккаунта платформы — заметку сохранить нельзя."

        ctx = await lookup_participant_stream(participant_account_id)
        if ctx is None:
            return None, None, None, "Не нашёл поток этого участника — он ещё не классифицирован в группе потока."

        mentor_account_id = await resolve_ory_id_from_chat(message.from_user.id)
        if mentor_account_id is None or await get_stream_reader_role(mentor_account_id, ctx.stream_id) is None:
            return None, None, None, f"Ты не числишься наставником или пилотом потока {ctx.stream_id}."

        participant_name = origin_user.full_name
        _active_participant[message.from_user.id] = _ActiveParticipant(
            stream_id=ctx.stream_id,
            participant_account_id=participant_account_id,
            participant_name=participant_name,
            set_at=time(),
        )
        return ctx.stream_id, participant_account_id, participant_name, None

    # Переслано собственное сообщение наставника (или автор скрыт настройками
    # приватности) — используем последнего однозначно определённого участника.
    active = _active_participant.get(message.from_user.id)
    if active is None or time() - active.set_at > _ACTIVE_PARTICIPANT_TTL_SECONDS:
        return None, None, None, (
            "Не могу понять, о ком заметка — сначала перешли сюда сообщение, "
            "которое написал сам участник, чтобы я его запомнил."
        )
    return active.stream_id, active.participant_account_id, active.participant_name, None


@mentorship_router.message(Command("mentor_note"), F.chat.type == "private")
async def cmd_mentor_note_dm(message: Message, command: CommandObject) -> None:
    """Личка: наставник сначала пересылает сообщение (участника или своё
    собственное) в чат с ботом, затем отвечает на него этой командой — тот же
    поток подтверждения карантина, что у групповой версии, но без группового
    контекста участника (WP-578, актуализация 19.09)."""
    target_message = message.reply_to_message
    if target_message is None:
        await message.reply(
            "Перешли сюда сообщение, которое хочешь сохранить, и ответь на него этой командой."
        )
        return

    body = (command.args or "").strip() or (target_message.text or target_message.caption or "").strip()
    if not body:
        await message.reply("В сообщении-цели нет текста — нечего сохранять.")
        return

    caller_account_id = await resolve_ory_id_from_chat(message.from_user.id)
    if caller_account_id is None:
        await message.reply("Не нашёл твой аккаунт платформы — сначала привяжи его (/link).")
        return

    stream_id, participant_account_id, participant_name, error = await _resolve_dm_participant_target(message, target_message)
    if error is not None:
        await message.reply(error)
        return

    await _stage_pending_note(
        message,
        mentor_account_id=caller_account_id,
        stream_id=stream_id,
        participant_account_id=participant_account_id,
        participant_name=participant_name,
        body=body,
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


_CARD_ENTRY_MAX_CHARS = 200
# Telegram sendMessage caps a message at 4096 characters (CLAUDE.md §10.21).
# Correspondence and notes are free text of arbitrary length (either the
# participant's own writing or what the mentor typed via /mentor_note) — the
# per-entry cap above bounds the common case, this is the last-resort net so
# a card with many long entries still sends instead of raising.
_CARD_MAX_CHARS = 4000


def _truncate_entry(text: str) -> str:
    text = text.strip().replace("\n", " ")
    if len(text) > _CARD_ENTRY_MAX_CHARS:
        return text[:_CARD_ENTRY_MAX_CHARS] + "…"
    return text


def _format_participant_card(card: dict, participant_name: str, stream_id: str) -> str:
    """Human-readable срез карточки участника (get_participant_card) —
    без parse_mode: текст переписки и заметок не наш, экранировать его под
    Markdown/HTML лишний риск (CLAUDE.md §10.2/§10.38), обычного текста для
    среза «на пальцах» достаточно."""
    lines = [f"👤 {participant_name} · поток {stream_id}"]

    manual = card.get("manualMinimum") or {}
    for prefix, key in (("🚩 ", "red_flag"), ("Зона: ", "screening_zone"), ("Проект: ", "project_note"), ("Дальше: ", "next_step")):
        value = manual.get(key)
        if value:
            lines.append(f"{prefix}{value}")

    if card.get("correspondenceEmpty"):
        lines.append("\n💬 Переписка: пока нет сообщений, которые бот распознал как адресованные тебе")
    else:
        lines.append("\n💬 Последняя переписка:")
        for entry in (card.get("recentTextCorrespondence") or [])[:5]:
            author = "наставник" if entry.get("author") == "mentor" else "участник"
            lines.append(f"· {author}: {_truncate_entry(entry.get('text') or '')}")
        meta_only = len(card.get("recentMetadataActivity") or [])
        if meta_only:
            lines.append(f"(+{meta_only} сообщений без сохранённого текста — нет согласия или запись историческая)")
    # correspondenceNote — фиксированное предупреждение сервиса
    # (get-participant-card.ts), не текст пустого состояния: классификатор
    # «адресовано наставнику» fail-closed, пустой список не доказывает, что
    # участник ничего не писал. Показываем всегда, не только при пустой
    # переписке — иначе смысл предупреждения теряется.
    correspondence_note = card.get("correspondenceNote")
    if correspondence_note:
        lines.append(f"ℹ️ {correspondence_note}")

    notes = card.get("recentNotes") or []
    if notes:
        lines.append("\n📝 Заметки наставника:")
        for note in notes[:5]:
            lines.append(f"· {_truncate_entry(note.get('body') or '')}")

    formatted = "\n".join(lines)
    if len(formatted) > _CARD_MAX_CHARS:
        formatted = formatted[:_CARD_MAX_CHARS] + "\n…(карточка обрезана, слишком длинная для одного сообщения)"
    return formatted


async def _send_participant_card(
    message: Message,
    mentor_account_id: str,
    stream_id: str,
    participant_account_id: str,
    participant_name: str,
    *,
    via_dm: bool,
) -> None:
    """Общий хвост group- и DM-версий /mentor_card: вызвать сервис и
    показать наставнику отформатированный срез (WP-578, третий клиент
    mentorship-service — архитектура заложена 17.09).

    Карточка несёт приватные данные участника (переписка, заметки) —
    `via_dm=True` (групповая команда) уходит наставнику личным сообщением,
    в группе остаётся только короткое подтверждение без содержимого
    (тот же принцип, что у F9 для usage-ошибок — не публиковать чужие
    личные данные в чат). `via_dm=False` (сама команда уже в личке с ботом)
    отвечает прямо, лишней пересылки не нужно."""
    async def _deliver(text: str) -> None:
        await (_tell_mentor(message, text) if via_dm else message.reply(text))

    try:
        card = await mentorship_service.get_participant_card(mentor_account_id, stream_id, participant_account_id)
    except MentorshipServiceError as e:
        logger.warning("[Mentorship] get_participant_card отклонён: %s — %s", e.kind, e)
        await _deliver(f"Не удалось получить карточку: {e}")
        return

    if card is None:
        await _deliver("Сервис карточки участника сейчас недоступен — попробуй позже.")
        return

    logger.info("[Mentorship] get_participant_card by=%s participant=%s stream=%s", mentor_account_id, participant_account_id, stream_id)
    formatted = _format_participant_card(card, participant_name, stream_id)
    if via_dm:
        # Подтверждать в группе можно только если личка реально доставлена —
        # иначе наставник видит "отправлено", а карточка с приватными
        # данными не пришла никуда (найдено ревью 25.09, воспроизведено).
        # Недоставка молчит, как и usage-ошибки этого же файла — тот же
        # признак "наставник ещё не открывал ЛС с ботом", не новая причина.
        if await _tell_mentor(message, formatted):
            await message.reply(f"Карточка участника {participant_name} отправлена тебе в личку.")
    else:
        await message.reply(formatted)


@mentorship_router.message(Command("mentor_card"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_mentor_card(message: Message) -> None:
    """Наставник отвечает командой на сообщение участника — бот показывает
    карточку этого участника, личным сообщением (WP-578 Ф3, новое требование
    пилота 23.09)."""
    reader = await _resolve_reader(message)
    if reader is None:
        return

    target_message = message.reply_to_message
    if target_message is None or target_message.from_user is None:
        await _tell_mentor(message, "Ответь этой командой на сообщение участника, чью карточку нужно показать.")
        return
    if target_message.from_user.id == message.from_user.id:
        await _tell_mentor(message, "Нельзя посмотреть карточку самого себя.")
        return

    stream_id = await _stream_of_group(message, reader)
    if stream_id is None:
        return

    participant_account_id = await resolve_ory_id_from_chat(target_message.from_user.id)
    if participant_account_id is None:
        await _tell_mentor(message, "У участника нет привязанного аккаунта платформы — карточка недоступна.")
        return

    await _send_participant_card(
        message, reader.account_id, stream_id, participant_account_id, target_message.from_user.full_name, via_dm=True
    )


@mentorship_router.message(Command("mentor_card"), F.chat.type == "private")
async def cmd_mentor_card_dm(message: Message) -> None:
    """Личка: наставник пересылает сюда сообщение участника (или своё
    собственное — тогда используется последний однозначно определённый
    участник этой же личной сессии) и отвечает на него этой командой — тот
    же резолвер, что у DM-версии /mentor_note (WP-578 Ф3)."""
    target_message = message.reply_to_message
    if target_message is None:
        await message.reply("Перешли сюда сообщение от участника (или своё, отправленное ему), и ответь на него этой командой.")
        return

    caller_account_id = await resolve_ory_id_from_chat(message.from_user.id)
    if caller_account_id is None:
        await message.reply("Не нашёл твой аккаунт платформы — сначала привяжи его (/link).")
        return

    stream_id, participant_account_id, participant_name, error = await _resolve_dm_participant_target(message, target_message)
    if error is not None:
        await message.reply(error)
        return

    await _send_participant_card(message, caller_account_id, stream_id, participant_account_id, participant_name, via_dm=False)
