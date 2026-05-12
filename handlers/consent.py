"""
/consent — управление согласием на трекинг для расчёта ступени.

WP-188 Ф17: writer для learning.tracking_consent через роль consent_writer (миграция 113).

Контракт:
  /consent              — status (показать текущее состояние)
  /consent opt-in       — privacy-текст + inline accept
  /consent opt-out      — UPDATE opt_in=FALSE (сохраняет историю)
  /consent revoke       — DELETE row (GDPR right to erasure, требует подтверждения)

Источник истины: Neon БД `learning.tracking_consent` (BC=learning, не персона —
это разрешение на агрегирование, не PII). Без opt_in=TRUE worker stage_evaluator
(WP-253 Блок 2) пропускает пользователя при ежедневном пересчёте ступени.

Privacy: см. B8.0 ToS/Privacy v1.0 (WP-212).
"""
from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandObject

from db.queries import get_intern
from db.queries.users import is_onboarded
from db.queries.consent import (
    get_consent,
    set_consent,
    revoke_consent,
    count_practice_events_30d,
    DEFAULT_SCOPE,
)
from helpers.dual_write import resolve_ory_id_from_chat
from i18n import t

logger = logging.getLogger(__name__)

consent_router = Router(name="consent")


_PRIVACY_URL = "https://system-school.ru/iwe/privacy"  # B8.0 публикация (WP-212)


def _scope_label(scope_name: str, lang: str = "ru") -> str:
    return {
        "stage_evaluation": "🎯 Оценка ступени мастерства",
        "club_activity": "🏛 Активность в клубе",
    }.get(scope_name, f"• {scope_name}")


def _activity_summary(events: dict[str, int]) -> str:
    """Краткая сводка событий + честная подсказка если активности мало."""
    total = events["practice"] + events["learning"]
    if total == 0:
        return (
            "📊 <b>Твоя активность за 30 дней:</b> пока пусто\n\n"
            "Чтобы платформа определила твою ступень, нужны действия, которые она умеет считать:\n"
            "  • <b>Уроки и тренировки</b> — /learn, /train в боте\n"
            "  • <b>Day Open / Day Close</b> — в Claude Code (если работаешь в IWE Template)\n"
            "  • <b>Заметки и фиксации</b> — через /me → Заметки\n\n"
            "Первый realистичный stage появится после 1–2 недель регулярных действий."
        )
    return (
        f"📊 <b>Твоя активность за 30 дней:</b>\n"
        f"  • Практика: {events['practice']} событий\n"
        f"  • Обучение: {events['learning']} событий\n\n"
        "Следующий пересчёт ступени — 04:35 МСК. Проверь /me, чтобы увидеть текущий stage."
    )


def _format_status_no_consent() -> str:
    return (
        "🔒 <b>Трекинг развития</b>\n\n"
        "Согласие на трекинг ещё не дано.\n\n"
        "Запусти /consent opt-in — платформа начнёт рассчитывать твою ступень "
        "мастерства по поведению (как часто практикуешь, что завершаешь, "
        "какие методы освоил).\n\n"
        "<i>⚠️ Важно: opt_in сам по себе не даёт stage. Нужны действия в боте "
        "(/learn, /train) или фиксация практики через Day Open/Close в IWE Template.</i>"
    )


def _format_status(consent, events: dict[str, int] | None = None, lang: str = "ru") -> str:
    if consent is None:
        return _format_status_no_consent()
    status_icon = "✅" if consent["opt_in"] else "🚫"
    status_text = "включён" if consent["opt_in"] else "отозван"
    scope_lines = "\n".join(f"  {_scope_label(s, lang)}" for s in (consent["scope"] or []))
    opted_at = consent["opted_at"].strftime("%Y-%m-%d %H:%M UTC")
    text = (
        f"{status_icon} <b>Трекинг развития:</b> {status_text}\n\n"
        f"<b>Что трекаем:</b>\n{scope_lines}\n\n"
        f"<i>Зафиксировано: {opted_at}</i>\n\n"
    )
    if consent["opt_in"] and events is not None:
        text += _activity_summary(events) + "\n\n"
    text += "Управление: /consent opt-in /consent opt-out /consent revoke"
    return text


def _privacy_text() -> str:
    return (
        "📋 <b>Согласие на трекинг развития</b>\n\n"
        "Чтобы платформа могла рассчитывать твою ступень мастерства "
        "(Случайный → Практикующий → Систематический → Дисциплинированный → Проактивный), "
        "мы анализируем поведенческие индикаторы:\n\n"
        "  • <b>Регулярность практики</b> — события day_plan, помодоро, IWE-сессии\n"
        "  • <b>Завершённость работ</b> — закрытые РП, фиксации, рефлексии\n"
        "  • <b>Освоенные методы</b> — Day Open/Close, Week Close, ОРЗ\n"
        "  • <b>Активность в клубе</b> — посты, комментарии (опционально)\n\n"
        "<b>Что мы НЕ делаем:</b>\n"
        "  • Не передаём данные третьим сторонам\n"
        "  • Не используем для рекламы\n"
        "  • Не анализируем содержимое заметок и текстов\n\n"
        f"Полные условия: <a href=\"{_PRIVACY_URL}\">Privacy Policy</a>\n\n"
        "Согласие можно отозвать в любой момент через /consent opt-out "
        "или удалить запись полностью через /consent revoke."
    )


def _accept_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Согласен, включить", callback_data="consent_accept"),
            InlineKeyboardButton(text="❌ Не сейчас", callback_data="consent_decline"),
        ],
    ])


def _revoke_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Да, удалить полностью", callback_data="consent_revoke_confirm"),
            InlineKeyboardButton(text="↩️ Отмена", callback_data="consent_revoke_cancel"),
        ],
    ])


async def _resolve_account(chat_id: int) -> tuple[dict | None, str | None]:
    """Returns (intern, account_id).

    Lookup account_id (Ory UUID) через persona.ory_identity — realtime,
    не из кэшированного intern. Это важно: после /link пользователь сразу
    должен мочь /consent, но intern dict обновляется позже (или вообще не
    содержит ory_id — только dt_user_id, который ставится при подключении ЦД).

    chat_id — для команд это message.chat.id; для callback'ов передавать
    callback.from_user.id (а не callback.message.chat.id) — future-proof
    на случай, если бот появится в группах (DM-only-инвариант не вечен).
    """
    intern = await get_intern(chat_id)
    account_id = await resolve_ory_id_from_chat(chat_id)
    return intern, account_id


def _link_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Привязать аккаунт сейчас", callback_data="consent_link_now")],
    ])


def _retry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="consent_retry_status")],
    ])


_NOT_LINKED_TEXT = (
    "🔒 <b>Сначала нужно привязать аккаунт Aisystant</b>\n\n"
    "Чтобы платформа считала твою ступень — она должна понимать, что ивенты "
    "(уроки, практика, заметки) принадлежат именно тебе. Это даёт привязка к Aisystant.\n\n"
    "<b>Нажми кнопку ниже</b> — бот найдёт твой Aisystant-аккаунт по Telegram username "
    "и привяжет за 2 секунды. После этого автоматически продолжим с согласием."
)


_LINKED_BUT_SYNCING_TEXT = (
    "⏳ <b>Аккаунт привязан, идёт синхронизация</b>\n\n"
    "Системе нужно 1–2 минуты, чтобы связать твой Telegram с профилем Aisystant. "
    "Нажми «🔄 Попробовать снова» через минуту. Если не получится — напиши Tseren."
)


@consent_router.message(Command("consent"))
async def cmd_consent(message: Message, command: CommandObject):
    """Управление consent. Подкоманды: status (default), opt-in, opt-out, revoke."""
    intern, account_id = await _resolve_account(message.from_user.id if message.from_user else message.chat.id)
    lang = (intern.get("language") if intern else "ru") or "ru"

    if not await is_onboarded(intern):
        await message.answer(t("profile.first_start", lang))
        return

    if not account_id:
        # Различить два сценария: (1) вообще не привязан Aisystant, (2) привязан, но Ory UUID
        # ещё не появился в persona.ory_identity (sync-задержка / не было OAuth-входа).
        from db.queries.aisystant import get_aisystant_id
        aisystant_id = await get_aisystant_id(message.chat.id)
        if aisystant_id:
            await message.answer(
                _LINKED_BUT_SYNCING_TEXT,
                parse_mode="HTML",
                reply_markup=_retry_keyboard(),
            )
        else:
            await message.answer(
                _NOT_LINKED_TEXT,
                parse_mode="HTML",
                reply_markup=_link_keyboard(),
            )
        return

    action = (command.args or "status").strip().lower()

    if action == "status":
        consent = await get_consent(account_id)
        events = None
        if consent and consent["opt_in"]:
            try:
                events = await count_practice_events_30d(account_id)
            except Exception as exc:
                logger.warning("[consent status] events count failed: %s", exc)
        await message.answer(_format_status(consent, events=events, lang=lang), parse_mode="HTML")
        return

    if action in ("opt-in", "opt_in", "in"):
        # GDPR: повторный opt-in не должен затирать opted_at первого согласия (аудит-метка).
        consent = await get_consent(account_id)
        if consent and consent["opt_in"]:
            opted_at = consent["opted_at"].strftime("%Y-%m-%d %H:%M UTC")
            await message.answer(
                f"✅ <b>Согласие уже активно.</b>\n\n"
                f"Зафиксировано: <i>{opted_at}</i>\n\n"
                "Управление: /consent /consent opt-out",
                parse_mode="HTML",
            )
            return
        await message.answer(_privacy_text(), parse_mode="HTML", reply_markup=_accept_keyboard(), disable_web_page_preview=True)
        return

    if action in ("opt-out", "opt_out", "out"):
        consent = await get_consent(account_id)
        if consent is None or not consent["opt_in"]:
            await message.answer("🚫 Согласие уже отозвано или не было дано.")
            return
        await set_consent(account_id, opt_in=False, scope=consent["scope"])
        await message.answer(
            "🚫 <b>Согласие отозвано.</b>\n\n"
            "Платформа больше не будет учитывать твои действия для расчёта ступени. "
            "История остаётся (для аудита). Полное удаление: /consent revoke",
            parse_mode="HTML",
        )
        return

    if action == "revoke":
        consent = await get_consent(account_id)
        if consent is None:
            await message.answer("🗑 Записи о согласии нет — удалять нечего.")
            return
        await message.answer(
            "⚠️ <b>Полное удаление согласия</b>\n\n"
            "Запись будет удалена. Действие необратимо — для повторного включения "
            "потребуется новый /consent opt-in\n\n"
            "Продолжить?",
            parse_mode="HTML",
            reply_markup=_revoke_keyboard(),
        )
        return

    await message.answer(
        "Неизвестная подкоманда. Доступные:\n"
        "/consent — текущее состояние\n"
        "/consent opt-in — дать согласие\n"
        "/consent opt-out — отозвать (сохраняет историю)\n"
        "/consent revoke — удалить запись",
        parse_mode="HTML",
    )


@consent_router.callback_query(F.data == "consent_accept")
async def on_consent_accept(callback: CallbackQuery):
    user_id = callback.from_user.id
    intern, account_id = await _resolve_account(user_id)
    if not account_id:
        await callback.answer("Аккаунт не привязан", show_alert=True)
        return
    try:
        consent = await set_consent(account_id, opt_in=True, scope=DEFAULT_SCOPE)
    except Exception as exc:
        logger.error("[consent_accept] account_id=%s: %s", account_id, exc)
        await callback.answer("Ошибка записи. Попробуй позже.", show_alert=True)
        return
    await callback.answer("Спасибо — согласие зафиксировано.")
    await callback.message.edit_text(
        "✅ <b>Согласие зафиксировано.</b>\n\n"
        "Платформа будет ежедневно (04:35 МСК) пересчитывать твою ступень мастерства.\n\n"
        "<b>Что считается:</b> /learn, /train (уроки), Day Open/Close в IWE Template, "
        "фиксации практики. Первый realистичный stage появится через 1–2 недели регулярной активности — "
        "проверь /consent через неделю, чтобы увидеть собранную статистику.\n\n"
        "Управление: /consent /consent opt-out",
        parse_mode="HTML",
    )
    logger.info("[consent] accept user_id=%s account_id=%s", user_id, account_id)


@consent_router.callback_query(F.data == "consent_decline")
async def on_consent_decline(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "👌 Без проблем — можешь вернуться к этому позже через /consent opt-in",
        parse_mode="HTML",
    )


@consent_router.callback_query(F.data == "consent_revoke_confirm")
async def on_consent_revoke_confirm(callback: CallbackQuery):
    intern, account_id = await _resolve_account(callback.from_user.id)
    if not account_id:
        await callback.answer("Аккаунт не привязан", show_alert=True)
        return
    try:
        deleted = await revoke_consent(account_id)
    except Exception as exc:
        logger.error("[consent_revoke] account_id=%s: %s", account_id, exc)
        await callback.answer("Ошибка удаления. Попробуй позже.", show_alert=True)
        return
    await callback.answer("Запись удалена.")
    msg = (
        "🗑 Запись о согласии удалена."
        if deleted else
        "ℹ️ Записи не было — удалять нечего."
    )
    await callback.message.edit_text(msg, parse_mode="HTML")


@consent_router.callback_query(F.data == "consent_revoke_cancel")
async def on_consent_revoke_cancel(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("↩️ Удаление отменено. Текущее состояние не изменилось.")


@consent_router.callback_query(F.data == "consent_link_now")
async def on_consent_link_now(callback: CallbackQuery):
    """Запустить привязку Aisystant прямо из /consent flow.

    После успешной привязки сразу показать privacy-текст для opt-in —
    не заставлять юзера повторно искать команду.
    """
    await callback.answer()
    chat_id = callback.from_user.id

    from db.queries.aisystant import get_aisystant_id, save_aisystant_link
    from clients import aisystant

    existing = await get_aisystant_id(chat_id)
    if existing:
        await callback.message.edit_text(
            "✅ Аккаунт Aisystant уже привязан. Проверяю синхронизацию…",
            parse_mode="HTML",
        )
    else:
        try:
            aisystant_id = await aisystant.find_user_by_tg(chat_id)
        except Exception as exc:
            logger.error("[consent_link_now] find_user_by_tg(%s): %s", chat_id, exc)
            await callback.message.edit_text(
                "❌ Не удалось найти Aisystant-аккаунт автоматически.\n\n"
                "Попробуй вручную: <b>/link</b> — там бот покажет ссылку для привязки.",
                parse_mode="HTML",
            )
            return
        if not aisystant_id:
            await callback.message.edit_text(
                "🔍 <b>Aisystant-аккаунт не найден по Telegram username</b>\n\n"
                "Возможно, у тебя ещё нет аккаунта или username не указан в Aisystant. "
                "Используй команду <b>/link</b> — она покажет ссылку для ручной привязки.",
                parse_mode="HTML",
            )
            return
        await save_aisystant_link(chat_id, aisystant_id)
        await callback.message.edit_text(
            "✅ <b>Аккаунт Aisystant найден и привязан.</b>\n\nПроверяю Ory-идентификатор…",
            parse_mode="HTML",
        )

    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        await callback.message.answer(
            _LINKED_BUT_SYNCING_TEXT,
            parse_mode="HTML",
            reply_markup=_retry_keyboard(),
        )
        return

    consent = await get_consent(account_id)
    if consent and consent["opt_in"]:
        opted_at = consent["opted_at"].strftime("%Y-%m-%d %H:%M UTC")
        await callback.message.answer(
            f"✅ <b>Согласие уже активно.</b>\n\nЗафиксировано: <i>{opted_at}</i>",
            parse_mode="HTML",
        )
        return

    await callback.message.answer(
        _privacy_text(),
        parse_mode="HTML",
        reply_markup=_accept_keyboard(),
        disable_web_page_preview=True,
    )


@consent_router.callback_query(F.data == "consent_from_onboarding")
async def on_consent_from_onboarding(callback: CallbackQuery):
    """Точка входа из онбординг-flow (WP-188 Ф17.8).

    Юзер только что прошёл /start и аккаунт Aisystant автопривязался — показываем
    privacy-текст и предлагаем opt-in без необходимости вводить /consent.
    """
    await callback.answer()
    chat_id = callback.from_user.id

    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        # Aisystant был «привязан» по флагу, но Ory UUID ещё не появился — sync-задержка.
        await callback.message.answer(
            _LINKED_BUT_SYNCING_TEXT,
            parse_mode="HTML",
            reply_markup=_retry_keyboard(),
        )
        return

    consent = await get_consent(account_id)
    if consent and consent["opt_in"]:
        opted_at = consent["opted_at"].strftime("%Y-%m-%d %H:%M UTC")
        await callback.message.answer(
            f"✅ Согласие уже активно. Зафиксировано: {opted_at}",
        )
        return

    await callback.message.answer(
        _privacy_text(),
        parse_mode="HTML",
        reply_markup=_accept_keyboard(),
        disable_web_page_preview=True,
    )


@consent_router.callback_query(F.data == "consent_retry_status")
async def on_consent_retry_status(callback: CallbackQuery):
    """Повторная попытка после ожидания Ory-синхронизации.

    Также сбрасывает negative-cache `resolve_ory_id_from_chat`, чтобы свежие
    данные из persona.ory_identity подтянулись.
    """
    await callback.answer()
    chat_id = callback.from_user.id

    # Сброс negative-cache: пользователь только что ожидал sync, кэш мог содержать stale None.
    from helpers.dual_write import _ory_cache
    _ory_cache.pop(chat_id, None)

    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        await callback.message.answer(
            "⏳ Идентификатор пока не появился. Попробуй ещё через минуту или напиши Tseren.",
            reply_markup=_retry_keyboard(),
        )
        return

    consent = await get_consent(account_id)
    if consent and consent["opt_in"]:
        opted_at = consent["opted_at"].strftime("%Y-%m-%d %H:%M UTC")
        await callback.message.answer(
            f"✅ <b>Согласие уже активно.</b>\n\nЗафиксировано: <i>{opted_at}</i>",
            parse_mode="HTML",
        )
        return

    await callback.message.answer(
        "✅ <b>Идентификатор подтянулся.</b>\n\nТеперь можно дать согласие:",
        parse_mode="HTML",
    )
    await callback.message.answer(
        _privacy_text(),
        parse_mode="HTML",
        reply_markup=_accept_keyboard(),
        disable_web_page_preview=True,
    )
