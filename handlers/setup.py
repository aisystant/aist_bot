"""
Setup Journey — маршрут оснащения T1→T4 (WP-349 Ф8-Ф10).

# see DP.SC.155, DP.SC.151

Команда /setup:
  Экран текущего тира с 2 кнопками: функциональная (развитие) + подключение (→ следующий тир).
  Re-entry: при повторном визите показывает тот же экран без изменений.

  T1 — Старт:     [Начать Марафон]  [Подписка → T2]
  T2 — Изучение:  [Открыть Ленту]   [Расширение → T3]
  T3 — Персонал.: [Личный гид]      [GitHub → T4]
  T4 — Созидание: [Открыть план]    [Пройти аттестацию]

double-tap protection: callback.answer() + edit_reply_markup(None) перед бизнес-логикой.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import PLATFORM_URLS
from core.tier_config import UITier
from core.tier_detector import detect_ui_tier
from db.queries import get_intern
from db.queries.cp_assessment import get_latest_cp_assessment
from db.queries.onboarding_journey import get_onboarding_state, write_last_nudge_at
from helpers.dual_write import resolve_ory_id_from_chat

logger = logging.getLogger(__name__)

setup_router = Router(name="setup")

_SUBSCRIPTION_URL = PLATFORM_URLS.get("subscription", "https://system-school.ru/open-endedness")
_BROWSER_GUIDE_URL = "https://docs.system-school.ru/ru/iwe/browser-extension"
_GITHUB_GUIDE_URL = "https://docs.system-school.ru/ru/iwe/github-setup"


# ─────────────────────────────────────────────
# Journey state (внутренние данные)
# ─────────────────────────────────────────────

def _compute_journey(tier: int, cp_row: dict | None, onb: dict | None) -> dict:
    """Вычислить состояние пути оснащения.

    tier_detector (detect_ui_tier) — единственный источник тира (DP.SC.155, CLAUDE.md §12).
    """
    stage = cp_row.get("stage") if cp_row else None
    return {
        "tier": tier,
        "stage": stage,
        "has_subscription": tier >= UITier.T2_LEARNING,
        "has_browser": tier >= UITier.T3_PERSONALIZATION,
        "has_github": tier >= UITier.T4_CREATION,
    }


# ─────────────────────────────────────────────
# Tier screens
# ─────────────────────────────────────────────

def _render_tier_screen(j: dict, is_returning: bool) -> str:
    tier = j["tier"]
    prefix = "С возвращением!\n\n" if is_returning else ""

    if tier >= UITier.T4_CREATION:
        return prefix + (
            "🚀 <b>Твой уровень на платформе — Т4 «Созидание»</b>, тебе доступны все инструменты платформы.\n\n"
            "• Рабочее окружение (VS Code): /connect\n"
            "• Личная база знаний в GitHub\n"
            "• Рабочий план /plan\n"
            "• Клуб /club — автопубликации из вашего GitHub\n"
            "• Баллы /points и статистика /me"
        )
    elif tier >= UITier.T3_PERSONALIZATION:
        return prefix + (
            "📚 <b>Твой уровень на платформе — Т3 «Персонализация»</b>, тебе доступна полная персональная настройка, включая статистику и гида.\n\n"
            "• Браузерный ассистент: /connect\n"
            "• Личный гид /guide — под твою ступень и домен\n"
            "• Баллы /points и статистика /me"
        )
    elif tier >= UITier.T2_LEARNING:
        return prefix + (
            "🌱 <b>Твой уровень на платформе — Т2 «Изучение»</b>, тебе доступны все руководства и знания платформы.\n\n"
            "• Лента /feed — ежедневные темы\n"
            "• Навигатор /navigator — выбор потока\n"
            "• Диагностика /diagnose\n"
            "• Баллы /points и статистика /me"
        )
    else:
        return prefix + (
            "🎯 <b>Твой уровень на платформе — Т1 «Старт»</b>, тебе доступны 14-дневный марафон и личная диагностика.\n\n"
            "14 дней × 20 мин/день, можно ставить паузу\n\n"
            "• Марафон — 14 занятий про системное мышление /learn\n"
            "• Диагностика ступени /diagnose"
        )


_WHAT_ELSE_BTN = InlineKeyboardButton(text="💡 Что ещё?", callback_data="setup:what_else")
_BACK_BTN = InlineKeyboardButton(text="← Назад", callback_data="setup:tier_back")

_WHAT_ELSE_COMMANDS = {
    UITier.T1: [
        "/support — поддержка",
        "/points — баллы",
        "/remind — ежедневное напоминание",
    ],
    UITier.T2_LEARNING: [
        "/knowledge — поиск по знаниям",
        "/progress — история занятий",
        "/rp — рабочие продукты",
    ],
    UITier.T3_PERSONALIZATION: [
        "/twin — цифровой двойник",
        "/me — статистика недели",
        "/navigator — Навигатор",
    ],
    UITier.T4_CREATION: [
        "/plan — план дня",
        "/knowledge_post — публикация постов",
        "/week_close — закрытие недели",
    ],
}


def _what_else_text(tier: int) -> str:
    key = (
        UITier.T4_CREATION if tier >= UITier.T4_CREATION else
        UITier.T3_PERSONALIZATION if tier >= UITier.T3_PERSONALIZATION else
        UITier.T2_LEARNING if tier >= UITier.T2_LEARNING else
        UITier.T1
    )
    cmds = _WHAT_ELSE_COMMANDS.get(key, [])
    lines = "\n".join(f"• {c}" for c in cmds)
    return f"💡 <b>Что ещё доступно на твоём уровне:</b>\n\n{lines}"


def _tier_keyboard(j: dict) -> InlineKeyboardMarkup:
    tier = j["tier"]

    if tier >= UITier.T4_CREATION:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 Открыть план /plan", callback_data="setup_action:plan"),
                InlineKeyboardButton(text="📊 Пройти аттестацию", callback_data="setup_action:assessment"),
            ],
            [_WHAT_ELSE_BTN],
        ])
    elif tier >= UITier.T3_PERSONALIZATION:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🧭 Открыть личный гид /guide", callback_data="setup_action:guide"),
                InlineKeyboardButton(text="🐙 Подключить GitHub → T4", callback_data="setup_step:github"),
            ],
            [_WHAT_ELSE_BTN],
        ])
    elif tier >= UITier.T2_LEARNING:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📖 Открыть Ленту /feed", callback_data="setup_action:feed"),
                InlineKeyboardButton(text="🔌 Установить расширение → T3", callback_data="setup_step:browser"),
            ],
            [_WHAT_ELSE_BTN],
        ])
    else:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📚 Начать Марафон", callback_data="setup_action:marathon"),
                InlineKeyboardButton(text="💳 Подписка → T2", url=_SUBSCRIPTION_URL),
            ],
            [_WHAT_ELSE_BTN],
        ])


# ─────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────

async def send_setup_screen(chat_id: int, message) -> None:
    """Show tier screen programmatically (post-consent, no /setup needed). DP.SC.157."""
    ory_uuid = await resolve_ory_id_from_chat(chat_id)
    if not ory_uuid:
        return

    tier, cp_row, onb = await asyncio.gather(
        detect_ui_tier(chat_id),
        get_latest_cp_assessment(ory_uuid),
        get_onboarding_state(ory_uuid),
    )
    j = _compute_journey(tier, cp_row, onb)

    text = _render_tier_screen(j, is_returning=False)
    kb = _tier_keyboard(j)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@setup_router.message(Command("setup"))
async def cmd_setup(message: Message) -> None:
    """Показать экран текущего тира T1–T4."""
    chat_id = message.chat.id

    ory_uuid = await resolve_ory_id_from_chat(chat_id)
    if not ory_uuid:
        await message.answer(
            "Для /setup привяжите аккаунт Aisystant через /link."
        )
        return

    tier, cp_row, onb, intern = await asyncio.gather(
        detect_ui_tier(chat_id),
        get_latest_cp_assessment(ory_uuid),
        get_onboarding_state(ory_uuid),
        get_intern(chat_id),
    )
    j = _compute_journey(tier, cp_row, onb)

    # Re-entry: пользователь уже был — показываем тот же экран с пометкой
    last_active = (intern or {}).get("last_active_date")
    today = date.today()
    is_returning = False
    if last_active is not None:
        if isinstance(last_active, datetime):
            last_active = last_active.date()
        elif isinstance(last_active, str):
            try:
                last_active = date.fromisoformat(last_active)
            except ValueError:
                last_active = None
        if last_active and last_active < today:
            is_returning = True

    text = _render_tier_screen(j, is_returning)
    kb = _tier_keyboard(j)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@setup_router.callback_query(F.data.startswith("setup_action:"))
async def on_setup_action(callback: CallbackQuery) -> None:
    """Функциональные кнопки тира: направить к нужной команде."""
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    action = callback.data.split(":", 1)[1]

    if action == "marathon":
        from handlers.marathon import start_marathon_flow
        await start_marathon_flow(callback.from_user.id, callback.message)
    elif action == "feed":
        await callback.message.answer(
            "Введите /feed — первый дайджест придёт сегодня в выбранное время."
        )
    elif action == "guide":
        await callback.message.answer(
            "Введите /guide — бот откроет ваш личный гид."
        )
    elif action == "plan":
        await callback.message.answer(
            "Введите /plan — ваш рабочий план на текущую неделю."
        )
    elif action == "assessment":
        await callback.message.answer(
            "Введите /assessment — оценит уровень освоения и предложит следующий поток."
        )
    else:
        logger.warning("[Setup] unknown action: %s chat_id=%s", action, callback.from_user.id)


@setup_router.callback_query(F.data.startswith("setup_step:"))
async def on_setup_step(callback: CallbackQuery) -> None:
    """Шаги подключения (browser, github) — инструкция с кнопкой подтверждения."""
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    chat_id = callback.from_user.id
    step = callback.data.split(":", 1)[1]

    ory_uuid = await resolve_ory_id_from_chat(chat_id)
    if ory_uuid:
        try:
            await write_last_nudge_at(ory_uuid)
        except Exception as e:
            logger.warning("[Setup] write_last_nudge_at failed: %s", e)

    if step == "browser":
        text = (
            "🔌 <b>Браузерный ассистент</b>\n\n"
            f"1. Откройте <a href='{_BROWSER_GUIDE_URL}'>инструкцию по установке</a>\n"
            "2. После установки выполните /connect в боте — "
            "бот проверит подключение и обновит ваш уровень."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Установил, выполняю /connect", callback_data="setup_browser_done"),
        ]])
    elif step == "github":
        text = (
            "🐙 <b>Репозиторий GitHub</b>\n\n"
            "Ваши знания останутся с вами — не в системе. "
            "GitHub-репозиторий — личная база, независимая от платформы.\n\n"
            f"1. Откройте <a href='{_GITHUB_GUIDE_URL}'>инструкцию по настройке</a>\n"
            "2. После настройки выполните /github в боте — "
            "бот проверит репозиторий и обновит ваш уровень."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Настроил, выполняю /github", callback_data="setup_github_done"),
        ]])
    else:
        logger.warning("[Setup] unknown step: %s chat_id=%s", step, chat_id)
        await callback.message.answer("Неизвестный шаг. Введите /setup снова.")
        return

    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@setup_router.callback_query(F.data.in_({"setup_browser_done", "setup_github_done"}))
async def on_setup_tool_done(callback: CallbackQuery) -> None:
    """Пользователь нажал «Готово» после инструкции — направить к команде."""
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    if callback.data == "setup_browser_done":
        await callback.message.answer(
            "Выполните /connect в этом чате — бот проверит подключение и обновит ваш уровень."
        )
    else:
        await callback.message.answer(
            "Выполните /github в этом чате — бот проверит репозиторий и обновит ваш уровень."
        )


@setup_router.callback_query(F.data == "setup:what_else")
async def on_what_else(callback: CallbackQuery) -> None:
    """«💡 Что ещё?» — показать tier-aware список команд. DP.SC.156."""
    await callback.answer()
    user_id = callback.from_user.id
    tier = await detect_ui_tier(user_id)
    text = _what_else_text(tier)
    kb = InlineKeyboardMarkup(inline_keyboard=[[_BACK_BTN]])
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)

    # domain_event: what_else_opened с тиром (fail-open: не блокирует UX)
    try:
        from db.queries.notifications import insert_domain_event_direct
        from db.queries.users import moscow_now
        from helpers.dual_write import resolve_ory_id_from_chat
        import uuid
        ory_uuid = await resolve_ory_id_from_chat(user_id)
        await insert_domain_event_direct(
            source="bot",
            external_id=str(uuid.uuid4()),
            event_type="what_else_opened",
            schema_version="1",
            occurred_at=moscow_now(),
            account_id=str(ory_uuid) if ory_uuid else None,
            payload={"tier": tier, "chat_id": user_id},
        )
    except Exception as exc:
        logger.warning("[Setup] what_else event log failed user_id=%s: %s", user_id, exc)


@setup_router.callback_query(F.data == "setup:tier_back")
async def on_tier_back(callback: CallbackQuery) -> None:
    """«← Назад» из «Что ещё?» — вернуться на tier-экран. DP.SC.156."""
    await callback.answer()
    await callback.message.delete()
    await send_setup_screen(callback.from_user.id, callback.message)
