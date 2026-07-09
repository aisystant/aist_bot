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
from aiogram.fsm.context import FSMContext

from db.queries import get_intern
from db.queries.consent import (
    get_consent,
    set_consent,
    revoke_consent,
    count_practice_events_30d,
    get_consent_grant,
    set_consent_grant,
    DEFAULT_SCOPE,
)
from clients.gateway_mcp import gateway_mcp
from clients.ory_oauth import ory_oauth
from config import ORY_CLIENT_ID
from helpers.dual_write import resolve_ory_id_from_chat
from i18n import t

logger = logging.getLogger(__name__)

consent_router = Router(name="consent")


_PRIVACY_URL = "https://docs.system-school.ru/iwe/privacy"  # B8.0 публикация (WP-212)


def _scope_label(scope_name: str, lang: str = "ru") -> str:
    return {
        "stage_evaluation": "Оценка ступени мастерства",
        "club_activity": "Активность в клубе",
        "text_analysis": "Анализ учебных текстов (тексты ответов на задания из LMS, посты в клубе, оценка мировоззрения)",
    }.get(scope_name, scope_name)


def _activity_summary(events: dict[str, int]) -> str:
    """Краткая сводка событий + честная подсказка если активности мало."""
    total = events["practice"] + events["learning"]
    if total == 0:
        return (
            "📊 <b>Твоя активность за 30 дней:</b> пока пусто\n\n"
            "Чтобы платформа определила твою ступень, нужны действия, которые она умеет считать:\n"
            '  • <b>Уроки и тренировки</b> — /learn, /train в боте\n'
            "  • <b>Day Open / Day Close</b> — в Claude Code (если работаешь в IWE Template)\n"
            '  • <b>Заметки и фиксации</b> — через /me → Заметки\n\n'
            "Первый реалистичный stage появится после 1–2 недель регулярных действий."
        )
    return (
        f"📊 <b>Твоя активность за 30 дней:</b>\n"
        f"  • Практика: {events['practice']} событий\n"
        f"  • Обучение: {events['learning']} событий\n\n"
        'Следующий пересчёт ступени — 04:35 МСК. Проверь /me, чтобы увидеть текущий stage.'
    )


def _format_status_no_consent() -> str:
    return (
        "🔒 <b>Трекинг развития</b>\n\n"
        "Согласие на трекинг ещё не дано.\n\n"
        "Запусти /consent opt-in — платформа начнёт рассчитывать твою ступень "
        "мастерства по поведению (как часто практикуешь, что завершаешь, "
        "какие методы освоил).\n\n"
        "<i>⚠️ Важно: opt_in сам по себе не даёт stage. Нужны действия в боте "
        '(/learn, /train) или фиксация практики через Day Open/Close в IWE Template.</i>'
    )


def _format_status(consent, events: dict[str, int] | None = None, lang: str = "ru", text_analysis: bool = False) -> str:
    if consent is None:
        return _format_status_no_consent()
    status_text = "включён" if consent["opt_in"] else "отозван"
    opted_at_short = consent["opted_at"].strftime("%d.%m.%Y")
    scope_lines = "\n".join(f"  • {_scope_label(s, lang)}" for s in (consent["scope"] or []))
    ta_text = "подключён" if text_analysis else "не подключён"

    text = (
        "<b>Ваши согласия</b>\n\n"
        f"  • Трекинг активности — {status_text} (с {opted_at_short})\n"
        f"{scope_lines}\n"
        f"  • Анализ учебных текстов — {ta_text}\n"
        "    <i>Хранение и анализ твоих ответов на задания из LMS и постов в клубе для оценки мировоззрения. Не влияет на ступень.</i>\n"
    )

    if consent["opt_in"] and events is not None:
        text += "\n" + _activity_summary(events)

    return text


def _privacy_text() -> str:
    return (
        "📋 <b>Согласие на трекинг развития</b>\n\n"
        "Чтобы платформа могла рассчитывать твою ступень мастерства "
        "(Случайный → Практикующий → Систематический → Дисциплинированный → Проактивный), "
        "мы анализируем поведенческие индикаторы:\n\n"
        "  • <b>Регулярность практики</b> — планирование дня, техника помодоро, сессии в IWE\n"
        "  • <b>Завершённость работ</b> — закрытые задачи и проекты, фиксации, рефлексии\n"
        "  • <b>Освоенные методы</b> — способы работы и саморазвития, которые формируют твой стиль жизни\n"
        "  • <b>Активность в клубе</b> — посты, комментарии (опционально)\n\n"
        "<b>Что мы НЕ делаем:</b>\n"
        "  • Не передаём данные третьим сторонам\n"
        "  • Не используем для рекламы\n"
        "  • Не анализируем тексты без твоего отдельного согласия (см. «Анализ учебных текстов» ниже)\n\n"
        f"Полные условия: <a href=\"{_PRIVACY_URL}\">Privacy Policy</a>\n\n"
        'Согласие можно отозвать в любой момент через <a href="https://t.me/aist_me_bot?start=consent_optout">/consent opt-out</a> '
        'или удалить запись полностью через <a href="https://t.me/aist_me_bot?start=consent_revoke">/consent revoke</a>.'
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


def _status_keyboard(consent, text_analysis: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура управления согласием в зависимости от состояния."""
    rows = []
    if consent is None:
        rows.append([InlineKeyboardButton(text="✅ Включить трекинг", callback_data="consent_goto_optin")])
    elif consent["opt_in"]:
        rows.append([
            InlineKeyboardButton(text="🚫 Отозвать", callback_data="consent_goto_optout"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data="consent_goto_revoke"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="✅ Включить снова", callback_data="consent_goto_optin"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data="consent_goto_revoke"),
        ])

    # WP-316 Ф9: text_analysis toggle
    if text_analysis:
        rows.append([InlineKeyboardButton(text="🚫 Отключить анализ текстов", callback_data="consent_text_analysis_off")])
    else:
        rows.append([InlineKeyboardButton(text="🧠 Включить анализ текстов", callback_data="consent_text_analysis_on")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


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


async def show_consent_optout(message: Message) -> None:
    """Точка входа для deep-link ?start=consent_optout (из onboarding.py)."""
    chat_id = message.chat.id
    intern, account_id = await _resolve_account(chat_id)
    if not account_id:
        from db.queries.aisystant import get_aisystant_id
        aisystant_id = await get_aisystant_id(chat_id)
        if aisystant_id:
            await message.answer(
                _LINKED_BUT_SYNCING_TEXT,
                parse_mode="HTML",
                reply_markup=await _retry_keyboard(chat_id),
            )
        else:
            await message.answer(
                _NOT_LINKED_TEXT,
                parse_mode="HTML",
                reply_markup=_link_keyboard(),
            )
        return
    consent = await get_consent(account_id)
    if consent is None or not consent["opt_in"]:
        await message.answer(
            "🚫 Согласие уже отозвано или не было дано.",
            reply_markup=_status_keyboard(consent),
        )
        return
    await set_consent(account_id, opt_in=False, scope=consent["scope"])
    updated = await get_consent(account_id)
    await message.answer(
        "🚫 <b>Согласие отозвано.</b>\n\n"
        "Платформа больше не будет учитывать твои действия для расчёта ступени. "
        "История остаётся (для аудита).",
        parse_mode="HTML",
        reply_markup=_status_keyboard(updated),
    )


async def show_consent_revoke(message: Message) -> None:
    """Точка входа для deep-link ?start=consent_revoke (из onboarding.py)."""
    chat_id = message.chat.id
    intern, account_id = await _resolve_account(chat_id)
    if not account_id:
        from db.queries.aisystant import get_aisystant_id
        aisystant_id = await get_aisystant_id(chat_id)
        if aisystant_id:
            await message.answer(
                _LINKED_BUT_SYNCING_TEXT,
                parse_mode="HTML",
                reply_markup=await _retry_keyboard(chat_id),
            )
        else:
            await message.answer(
                _NOT_LINKED_TEXT,
                parse_mode="HTML",
                reply_markup=_link_keyboard(),
            )
        return
    consent = await get_consent(account_id)
    if consent is None:
        await message.answer(
            "🗑 Записи о согласии нет — удалять нечего.",
            reply_markup=_status_keyboard(None),
        )
        return
    await message.answer(
        "⚠️ <b>Полное удаление согласия</b>\n\n"
        "Запись будет удалена. Действие необратимо — для повторного включения "
        "потребуется новый /consent opt-in\n\n"
        "Продолжить?",
        parse_mode="HTML",
        reply_markup=_revoke_keyboard(),
    )


def _link_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Привязать аккаунт сейчас", callback_data="consent_link_now")],
    ])


async def _retry_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Клавиатура экрана «синхронизация»: реальный вход через Ory + retry.

    Кнопка входа показывается только если Ory OAuth настроен (ORY_CLIENT_ID) и
    удалось получить auth_url — иначе graceful fallback на старую кнопку retry.
    """
    buttons = []
    if ORY_CLIENT_ID:
        try:
            auth_url, _ = await ory_oauth.get_authorization_url(chat_id)
            buttons.append([InlineKeyboardButton(text="🔑 Войти через Aisystant", url=auth_url)])
        except Exception as exc:
            logger.warning("[_retry_keyboard] get_authorization_url(%s) failed: %s", chat_id, exc)
    buttons.append([InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="consent_retry_status")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


_NOT_LINKED_TEXT = (
    "🔒 <b>Сначала нужно привязать аккаунт Aisystant</b>\n\n"
    "Чтобы платформа считала твою ступень — она должна понимать, что ивенты "
    "(уроки, практика, заметки) принадлежат именно тебе. Это даёт привязка к Aisystant.\n\n"
    "<b>Нажми кнопку ниже</b> — бот найдёт твой Aisystant-аккаунт по Telegram username "
    "и привяжет за 2 секунды. После этого автоматически продолжим с согласием."
)


_LINKED_BUT_SYNCING_TEXT = (
    "⏳ <b>Аккаунт привязан, идёт синхронизация</b>\n\n"
    "Иногда синхронизация не завершается сама — если ты ещё не входил через "
    "Aisystant (Ory), это самый быстрый способ. Уже входил? Нажми «🔄 Попробовать снова»."
)


async def show_consent_optin(message: Message) -> None:
    """Show consent opt-in privacy screen. Called from deep links (e.g. ?start=consent).

    WP-188 Ф17: handles all states — not linked / syncing / already opted-in / fresh.
    see: scenario-02-13-consent.md §5 п.3
    """
    chat_id = message.from_user.id if message.from_user else message.chat.id
    intern, account_id = await _resolve_account(chat_id)
    lang = (intern.get("language") if intern else "ru") or "ru"

    if not intern:
        await message.answer(t("profile.first_start", lang))
        return

    if not account_id:
        from db.queries.aisystant import get_aisystant_id
        aisystant_id = await get_aisystant_id(chat_id)
        if aisystant_id:
            await message.answer(_LINKED_BUT_SYNCING_TEXT, parse_mode="HTML", reply_markup=await _retry_keyboard(chat_id))
        else:
            await message.answer(_NOT_LINKED_TEXT, parse_mode="HTML", reply_markup=_link_keyboard())
        return

    consent = await get_consent(account_id)
    if consent and consent["opt_in"]:
        text_analysis = await get_consent_grant(account_id, "text_analysis")
        await message.answer(
            _format_status(consent, text_analysis=text_analysis),
            parse_mode="HTML",
            reply_markup=_status_keyboard(consent, text_analysis=text_analysis),
        )
        return

    await message.answer(_privacy_text(), parse_mode="HTML", reply_markup=_accept_keyboard(), disable_web_page_preview=True)


@consent_router.message(Command("consent"))
async def cmd_consent(message: Message, command: CommandObject):
    """Управление consent. Подкоманды: status (default), opt-in, opt-out, revoke."""
    chat_id = message.chat.id
    intern, account_id = await _resolve_account(message.from_user.id if message.from_user else chat_id)
    lang = (intern.get("language") if intern else "ru") or "ru"

    if not intern:
        await message.answer(t("profile.first_start", lang))
        return

    if not account_id:
        # Различить два сценария: (1) вообще не привязан Aisystant, (2) привязан, но Ory UUID
        # ещё не появился в persona.ory_identity (sync-задержка / не было OAuth-входа).
        from db.queries.aisystant import get_aisystant_id
        aisystant_id = await get_aisystant_id(chat_id)
        if aisystant_id:
            await message.answer(
                _LINKED_BUT_SYNCING_TEXT,
                parse_mode="HTML",
                reply_markup=await _retry_keyboard(chat_id),
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
        text_analysis = await get_consent_grant(account_id, "text_analysis")
        events = None
        if consent and consent["opt_in"]:
            try:
                events = await count_practice_events_30d(account_id)
            except Exception as exc:
                logger.warning("[consent status] events count failed: %s", exc)
        await message.answer(
            _format_status(consent, events=events, lang=lang, text_analysis=text_analysis),
            parse_mode="HTML",
            reply_markup=_status_keyboard(consent, text_analysis=text_analysis),
        )
        return

    if action in ("opt-in", "opt_in", "in"):
        # GDPR: повторный opt-in не должен затирать opted_at первого согласия (аудит-метка).
        consent = await get_consent(account_id)
        if consent and consent["opt_in"]:
            opted_at = consent["opted_at"].strftime("%Y-%m-%d %H:%M UTC")
            await message.answer(
                f"✅ <b>Согласие уже активно.</b>\n\n"
                f"Зафиксировано: <i>{opted_at}</i>",
                parse_mode="HTML",
                reply_markup=_status_keyboard(consent),
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
        updated_consent = await get_consent(account_id)
        await message.answer(
            "🚫 <b>Согласие отозвано.</b>\n\n"
            "Платформа больше не будет учитывать твои действия для расчёта ступени. "
            "История остаётся (для аудита).",
            parse_mode="HTML",
            reply_markup=_status_keyboard(updated_consent),
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
            'потребуется новый /consent opt-in\n\n'
            "Продолжить?",
            parse_mode="HTML",
            reply_markup=_revoke_keyboard(),
        )
        return

    consent = await get_consent(account_id)
    await message.answer(
        _format_status(consent, lang=lang),
        parse_mode="HTML",
        reply_markup=_status_keyboard(consent),
    )


def _intent_keyboard() -> InlineKeyboardMarkup:
    """Экран выбора намерения после первого consent (Вариант Б, WP-349 Ф16б)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Начать марафон", callback_data="intent:marathon"),
            InlineKeyboardButton(text="Узнать ступень", callback_data="intent:diagnose"),
        ],
        [
            InlineKeyboardButton(text="Обзор платформы", callback_data="intent:setup"),
        ],
    ])


async def _send_intent_screen(message) -> None:
    """Отправить экран выбора намерения после первого consent."""
    await message.answer(
        "С чего начнём?",
        reply_markup=_intent_keyboard(),
    )


@consent_router.callback_query(F.data == "intent:marathon")
async def on_intent_marathon(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_reply_markup(None)
    from handlers.marathon import start_marathon_flow
    await start_marathon_flow(callback.from_user.id, callback.message)


@consent_router.callback_query(F.data == "intent:diagnose")
async def on_intent_diagnose(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(None)
    from handlers.diagnose import cmd_diagnose
    await cmd_diagnose(callback.message, state)


@consent_router.callback_query(F.data == "intent:setup")
async def on_intent_setup(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_reply_markup(None)
    chat_id = callback.from_user.id
    try:
        from handlers.setup import send_setup_screen
        await send_setup_screen(chat_id, callback.message)
    except Exception as exc:
        logger.warning("[intent:setup] setup_screen failed chat_id=%s: %s", chat_id, exc)


@consent_router.callback_query(F.data == "consent_accept")
async def on_consent_accept(callback: CallbackQuery):
    user_id = callback.from_user.id
    intern, account_id = await _resolve_account(user_id)
    if not account_id:
        await callback.answer("Аккаунт не привязан", show_alert=True)
        return

    # WP-349 Ф30 + WP-457 Ф10 (consent write-path fix): data_analysis — единственный
    # писатель learning-context-service через шлюз; text_analysis — единственный
    # писатель бот напрямую (set_consent_grant). Было: оба scope одним вызовом шлюза
    # с ошибочными scope из чужой константы DEFAULT_SCOPE (stage_evaluation/club_activity —
    # легаси-механизм /consent, не имеет отношения к data_analysis/text_analysis).
    # Локальный фолбэк на сбой шлюза убран: gateway-mcp уже делает retry+backoff
    # (WP-457 Ф12, K3-митигация) — тихая запись в другую таблицу под видом успеха
    # хуже честного отказа для юридически значимого согласия.
    try:
        result = await gateway_mcp.grant_consent(
            telegram_user_id=user_id,
            agreed=True,
            scopes=["data_analysis"],
        )
        if result is None or not result.get("success"):
            logger.error("[consent_accept] gateway grant_consent failed for %s: %s", user_id, result)
            await callback.answer("Ошибка записи. Попробуй позже.", show_alert=True)
            return
        await set_consent_grant(account_id, "text_analysis", granted=True)
        # Читаем обратно из БД для UI (клавиатура старого /consent, см. _status_keyboard)
        consent = await get_consent(account_id)
    except Exception as exc:
        logger.error("[consent_accept] account_id=%s: %s", account_id, exc)
        await callback.answer("Ошибка записи. Попробуй позже.", show_alert=True)
        return
    await callback.answer("Спасибо — согласие зафиксировано.")
    await callback.message.edit_text(
        "✅ <b>Согласие зафиксировано.</b>\n\n"
        "Платформа будет ежедневно (04:35 МСК) пересчитывать твою ступень мастерства "
        "по всем источникам активности:\n\n"
        "  • <b>Бот</b> — уроки (/learn), тренировки (/train), фиксации практики\n"
        "  • <b>Учебная платформа</b> — завершённые уроки и тренировки\n"
        "  • <b>Клуб</b> — посты, комментарии, участие\n"
        "  • <b>Рабочая среда IWE</b> — планирование, рефлексии, закрытые задачи и проекты\n\n"
        "Первый реалистичный stage появится через 1–2 недели регулярной активности. "
        "Проверь статус через неделю — команда /consent.",
        parse_mode="HTML",
        reply_markup=_status_keyboard(consent),
    )
    logger.info("[consent] accept user_id=%s account_id=%s", user_id, account_id)

    # Ф20 (WP-349): записать referral_source если пришёл по реф-ссылке
    try:
        ref_uuid = (intern.get('current_context') or {}).get('referral_uuid') if intern else None
        if ref_uuid:
            from db.queries.onboarding_journey import write_referral_source
            written = await write_referral_source(account_id, ref_uuid)
            if written:
                logger.info("[Ф20] referral_source written user_id=%s ref=%s", user_id, ref_uuid[:8])
            else:
                logger.warning("[Ф20] referral_source not written (already set?) user_id=%s", user_id)
    except Exception as exc:
        logger.warning("[Ф20] referral_source write failed: %s", exc)

    # Post-consent: экран выбора намерения (Вариант Б, WP-349 Ф16б)
    try:
        await _send_intent_screen(callback.message)
    except Exception as exc:
        logger.warning("[consent] intent_screen failed user_id=%s: %s", user_id, exc)


@consent_router.callback_query(F.data == "consent_decline")
async def on_consent_decline(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "👌 Без проблем — можешь вернуться к этому позже.",
        parse_mode="HTML",
        reply_markup=_status_keyboard(None),
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
    await callback.message.edit_text(msg, parse_mode="HTML", reply_markup=_status_keyboard(None))


@consent_router.callback_query(F.data == "consent_revoke_cancel")
async def on_consent_revoke_cancel(callback: CallbackQuery):
    await callback.answer()
    chat_id = callback.from_user.id
    account_id = await resolve_ory_id_from_chat(chat_id)
    consent = await get_consent(account_id) if account_id else None
    text_analysis = await get_consent_grant(account_id, "text_analysis") if account_id else False
    await callback.message.edit_text(
        _format_status(consent, text_analysis=text_analysis),
        parse_mode="HTML",
        reply_markup=_status_keyboard(consent, text_analysis=text_analysis),
    )


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
            reply_markup=await _retry_keyboard(chat_id),
        )
        return

    consent = await get_consent(account_id)
    if consent and consent["opt_in"]:
        opted_at = consent["opted_at"].strftime("%Y-%m-%d %H:%M UTC")
        await callback.message.answer(
            f"✅ <b>Согласие уже активно.</b>\n\nЗафиксировано: <i>{opted_at}</i>",
            parse_mode="HTML",
            reply_markup=_status_keyboard(consent),
        )
        return

    await callback.message.answer(
        _privacy_text(),
        parse_mode="HTML",
        reply_markup=_accept_keyboard(),
        disable_web_page_preview=True,
    )


@consent_router.callback_query(F.data == "consent_goto_optin")
async def on_consent_goto_optin(callback: CallbackQuery):
    """Inline-кнопка: перейти к opt-in (privacy-текст)."""
    await callback.answer()
    chat_id = callback.from_user.id
    intern, account_id = await _resolve_account(chat_id)
    if not account_id:
        await callback.message.answer(
            _NOT_LINKED_TEXT,
            parse_mode="HTML",
            reply_markup=_link_keyboard(),
        )
        return
    consent = await get_consent(account_id)
    if consent and consent["opt_in"]:
        opted_at = consent["opted_at"].strftime("%Y-%m-%d %H:%M UTC")
        await callback.message.edit_text(
            f"✅ <b>Согласие уже активно.</b>\n\nЗафиксировано: <i>{opted_at}</i>",
            parse_mode="HTML",
            reply_markup=_status_keyboard(consent),
        )
        return
    await callback.message.edit_text(
        _privacy_text(),
        parse_mode="HTML",
        reply_markup=_accept_keyboard(),
        disable_web_page_preview=True,
    )


@consent_router.callback_query(F.data == "consent_goto_optout")
async def on_consent_goto_optout(callback: CallbackQuery):
    """Inline-кнопка: отозвать согласие (opt-out)."""
    await callback.answer()
    chat_id = callback.from_user.id
    intern, account_id = await _resolve_account(chat_id)
    if not account_id:
        await callback.message.answer("Аккаунт не привязан — попробуй /link.")
        return
    consent = await get_consent(account_id)
    if consent is None or not consent["opt_in"]:
        await callback.message.edit_text(
            "Согласие уже отозвано или не было дано.",
            reply_markup=_status_keyboard(consent),
        )
        return

    # WP-349 Ф30: отзыв consent через gateway MCP (cross-channel)
    try:
        result = await gateway_mcp.grant_consent(
            telegram_user_id=chat_id,
            agreed=False,
            scopes=list(DEFAULT_SCOPE) if DEFAULT_SCOPE else ["data_analysis", "text_analysis"],
        )
        if result is None:
            logger.warning("[consent_goto_optout] gateway grant_consent returned None for %s", chat_id)
    except Exception as exc:
        logger.warning("[consent_goto_optout] gateway call failed: %s", exc)

    # Fallback: прямая запись в БД (gateway может не поддерживать agreed=false)
    await set_consent(account_id, opt_in=False, scope=consent["scope"])
    await callback.message.edit_text(
        "🚫 <b>Согласие отозвано.</b>\n\n"
        "Платформа больше не будет учитывать твои действия для расчёта ступени. "
        "История остаётся (для аудита).",
        parse_mode="HTML",
        reply_markup=_status_keyboard(await get_consent(account_id)),
    )


@consent_router.callback_query(F.data == "consent_goto_revoke")
async def on_consent_goto_revoke(callback: CallbackQuery):
    """Inline-кнопка: запрос подтверждения удаления записи."""
    await callback.answer()
    chat_id = callback.from_user.id
    intern, account_id = await _resolve_account(chat_id)
    if not account_id:
        await callback.answer("Аккаунт не привязан", show_alert=True)
        return
    consent = await get_consent(account_id)
    if consent is None:
        await callback.message.edit_text(
            "🗑 Записи о согласии нет — удалять нечего.",
            reply_markup=_status_keyboard(None),
        )
        return
    await callback.message.edit_text(
        "⚠️ <b>Полное удаление согласия</b>\n\n"
        "Запись будет удалена. Действие необратимо — для повторного включения "
        "потребуется новый /consent opt-in\n\n"
        "Продолжить?",
        parse_mode="HTML",
        reply_markup=_revoke_keyboard(),
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
            reply_markup=await _retry_keyboard(chat_id),
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
            _LINKED_BUT_SYNCING_TEXT,
            parse_mode="HTML",
            reply_markup=await _retry_keyboard(chat_id),
        )
        return

    consent = await get_consent(account_id)
    text_analysis = await get_consent_grant(account_id, "text_analysis")
    if consent and consent["opt_in"]:
        opted_at = consent["opted_at"].strftime("%Y-%m-%d %H:%M UTC")
        await callback.message.answer(
            f"✅ <b>Согласие уже активно.</b>\n\nЗафиксировано: <i>{opted_at}</i>",
            parse_mode="HTML",
            reply_markup=_status_keyboard(consent, text_analysis=text_analysis),
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


# WP-316 Ф9: text_analysis consent toggle
@consent_router.callback_query(F.data == "consent_text_analysis_on")
async def on_text_analysis_on(callback: CallbackQuery):
    """Включить text_analysis consent (hard opt-in)."""
    await callback.answer()
    chat_id = callback.from_user.id
    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        await callback.message.answer("Аккаунт не привязан — попробуй /link.")
        return
    try:
        await set_consent_grant(account_id, "text_analysis", granted=True)
    except Exception as exc:
        logger.error("[consent_text_analysis_on] set_grant failed account_id=%s: %s", account_id, exc)
        await callback.message.answer("Ошибка записи. Попробуй /consent ещё раз.")
        return
    consent = await get_consent(account_id)
    events = None
    if consent and consent["opt_in"]:
        try:
            events = await count_practice_events_30d(account_id)
        except Exception:
            pass
    try:
        await callback.message.edit_text(
            _format_status(consent, events=events, text_analysis=True),
            parse_mode="HTML",
            reply_markup=_status_keyboard(consent, text_analysis=True),
        )
    except Exception as exc:
        logger.warning("[consent_text_analysis_on] edit_text failed: %s", exc)


@consent_router.callback_query(F.data == "consent_text_analysis_off")
async def on_text_analysis_off(callback: CallbackQuery):
    """Отключить text_analysis consent."""
    await callback.answer()
    chat_id = callback.from_user.id
    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        await callback.message.answer("Аккаунт не привязан — попробуй /link.")
        return
    try:
        await set_consent_grant(account_id, "text_analysis", granted=False)
    except Exception as exc:
        logger.error("[consent_text_analysis_off] set_grant failed account_id=%s: %s", account_id, exc)
        await callback.message.answer("Ошибка записи. Попробуй /consent ещё раз.")
        return
    consent = await get_consent(account_id)
    events = None
    if consent and consent["opt_in"]:
        try:
            events = await count_practice_events_30d(account_id)
        except Exception:
            pass
    try:
        await callback.message.edit_text(
            _format_status(consent, events=events, text_analysis=False),
            parse_mode="HTML",
            reply_markup=_status_keyboard(consent, text_analysis=False),
        )
    except Exception as exc:
        logger.warning("[consent_text_analysis_off] edit_text failed: %s", exc)
