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


def _format_status(consent, lang: str = "ru") -> str:
    if consent is None:
        return (
            "🔒 <b>Трекинг развития</b>\n\n"
            "Согласие на трекинг ещё не дано.\n\n"
            "Запусти <code>/consent opt-in</code>, чтобы платформа рассчитывала твою ступень "
            "мастерства по поведенческим индикаторам (FORM.089)."
        )
    status_icon = "✅" if consent["opt_in"] else "🚫"
    status_text = "включён" if consent["opt_in"] else "отозван"
    scope_lines = "\n".join(f"  {_scope_label(s, lang)}" for s in (consent["scope"] or []))
    opted_at = consent["opted_at"].strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{status_icon} <b>Трекинг развития:</b> {status_text}\n\n"
        f"<b>Что трекаем:</b>\n{scope_lines}\n\n"
        f"<i>Зафиксировано: {opted_at}</i>\n\n"
        "Управление: <code>/consent opt-in</code> / <code>/consent opt-out</code> / <code>/consent revoke</code>"
    )


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
        f"Полные условия: <a href=\"{_PRIVACY_URL}\">Privacy Policy v1.0</a>\n\n"
        "Согласие можно отозвать в любой момент через <code>/consent opt-out</code> "
        "или удалить запись полностью через <code>/consent revoke</code>."
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


async def _resolve_account(message: Message) -> tuple[dict | None, str | None]:
    """Returns (intern, account_id).

    Lookup account_id (Ory UUID) через persona.ory_identity — realtime,
    не из кэшированного intern. Это важно: после /link пользователь сразу
    должен мочь /consent, но intern dict обновляется позже (или вообще не
    содержит ory_id — только dt_user_id, который ставится при подключении ЦД).
    """
    chat_id = message.chat.id
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
    "⏳ <b>Аккаунт Aisystant привязан, но идёт синхронизация</b>\n\n"
    "Идентификатор появится в системе в течение 1–2 минут. "
    "Это нужно, потому что bot и stage_evaluator используют разные слои identity "
    "(Telegram → Aisystant → Ory UUID).\n\n"
    "Нажми «🔄 Попробовать снова» через минуту — если не получилось, напиши Tseren."
)


@consent_router.message(Command("consent"))
async def cmd_consent(message: Message, command: CommandObject):
    """Управление consent. Подкоманды: status (default), opt-in, opt-out, revoke."""
    intern, account_id = await _resolve_account(message)
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
        await message.answer(_format_status(consent, lang), parse_mode="HTML")
        return

    if action in ("opt-in", "opt_in", "in"):
        # GDPR: повторный opt-in не должен затирать opted_at первого согласия (аудит-метка).
        consent = await get_consent(account_id)
        if consent and consent["opt_in"]:
            opted_at = consent["opted_at"].strftime("%Y-%m-%d %H:%M UTC")
            await message.answer(
                f"✅ <b>Согласие уже активно.</b>\n\n"
                f"Зафиксировано: <i>{opted_at}</i>\n\n"
                "Управление: <code>/consent</code> (status) / <code>/consent opt-out</code> (отозвать).",
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
            "Worker stage_evaluator больше не будет обрабатывать твои данные. "
            "История остаётся для аудита. Полное удаление: <code>/consent revoke</code>.",
            parse_mode="HTML",
        )
        return

    if action == "revoke":
        consent = await get_consent(account_id)
        if consent is None:
            await message.answer("🗑 Записи о согласии нет — удалять нечего.")
            return
        await message.answer(
            "⚠️ <b>Полное удаление записи о согласии (GDPR right to erasure)</b>\n\n"
            "Будет удалена запись из <code>learning.tracking_consent</code>. "
            "Это действие необратимо. Для повторного включения трекинга потребуется новый <code>/consent opt-in</code>.\n\n"
            "Продолжить?",
            parse_mode="HTML",
            reply_markup=_revoke_keyboard(),
        )
        return

    await message.answer(
        "Неизвестная подкоманда. Доступные:\n"
        "<code>/consent</code> — текущее состояние\n"
        "<code>/consent opt-in</code> — дать согласие\n"
        "<code>/consent opt-out</code> — отозвать (сохраняет историю)\n"
        "<code>/consent revoke</code> — удалить запись (GDPR)",
        parse_mode="HTML",
    )


@consent_router.callback_query(F.data == "consent_accept")
async def on_consent_accept(callback: CallbackQuery):
    intern, account_id = await _resolve_account(callback.message)
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
        "Теперь worker stage_evaluator будет ежедневно (04:35 МСК) пересчитывать "
        "твою ступень мастерства по поведенческим индикаторам.\n\n"
        "Управление: <code>/consent</code> (status) / <code>/consent opt-out</code> (отозвать).",
        parse_mode="HTML",
    )
    logger.info("[consent] accept chat_id=%s account_id=%s", callback.message.chat.id, account_id)


@consent_router.callback_query(F.data == "consent_decline")
async def on_consent_decline(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "👌 Без проблем — можешь вернуться к этому позже через <code>/consent opt-in</code>.",
        parse_mode="HTML",
    )


@consent_router.callback_query(F.data == "consent_revoke_confirm")
async def on_consent_revoke_confirm(callback: CallbackQuery):
    intern, account_id = await _resolve_account(callback.message)
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
    chat_id = callback.message.chat.id

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


@consent_router.callback_query(F.data == "consent_retry_status")
async def on_consent_retry_status(callback: CallbackQuery):
    """Повторная попытка после ожидания Ory-синхронизации.

    Также сбрасывает negative-cache `resolve_ory_id_from_chat`, чтобы свежие
    данные из persona.ory_identity подтянулись.
    """
    await callback.answer()
    chat_id = callback.message.chat.id

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
