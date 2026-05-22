"""
Setup Journey — маршрут оснащения T1→T4 (WP-349 Ф8-Ф10).

# see DP.SC.155, DP.SC.151

Команда /setup:
  Ф8: дашборд прогресса (tier_detector + cp_assessments + onboarding_state через gather)
  Ф9: guided flow — per-step сообщение «зачем» + CTA (double-tap protection)
  Ф10: re-entry — пользователь открыл /setup не сегодня → «Продолжим с [шаг]?»

double-tap protection: callback.answer() + edit_reply_markup(None) перед бизнес-логикой.
Каждый CTA-клик пишет last_nudge_at (cooldown-sync с onboarding_controller).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

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

# Русские имена шагов для re-entry
_STEP_NAMES = {
    "diagnose": "диагностики ступени",
    "subscribe": "оформления подписки",
    "browser": "подключения расширения",
    "github": "подключения GitHub",
}


# ─────────────────────────────────────────────
# Journey logic (Ф8)
# ─────────────────────────────────────────────

def _compute_journey(tier: int, cp_row: dict | None, onb: dict | None) -> dict:
    """Вычислить состояние пути оснащения из свежих данных."""
    stage = cp_row.get("stage") if cp_row else None
    has_diagnosis = stage is not None

    has_subscription = tier >= UITier.T2_LEARNING
    has_browser = tier >= UITier.T3_PERSONALIZATION
    has_github = tier >= UITier.T4_CREATION

    if not has_diagnosis:
        next_step = "diagnose"
    elif not has_subscription:
        next_step = "subscribe"
    elif not has_browser:
        next_step = "browser"
    elif not has_github:
        next_step = "github"
    else:
        next_step = "complete"

    return {
        "tier": tier,
        "stage": stage,
        "has_diagnosis": has_diagnosis,
        "has_subscription": has_subscription,
        "has_browser": has_browser,
        "has_github": has_github,
        "next_step": next_step,
    }


def _render_dashboard(j: dict) -> str:
    tick = "✅"
    cross = "⬜"
    stage_line = f"{tick} Ступень: {j['stage']}" if j["stage"] else f"{cross} Диагностика ступени"
    sub_line = f"{tick} Подписка БР" if j["has_subscription"] else f"{cross} Подписка БР"
    browser_line = f"{tick} Браузер-расширение" if j["has_browser"] else f"{cross} Браузер-расширение (T3)"
    github_line = f"{tick} Репозиторий GitHub" if j["has_github"] else f"{cross} Репозиторий GitHub (T4)"
    lines = [
        "📍 <b>Ваш путь оснащения: T1 → T4</b>",
        "",
        f"{tick} Аккаунт привязан",
        stage_line,
        sub_line,
        browser_line,
        github_line,
    ]
    if j["next_step"] == "complete":
        lines += ["", "🎉 Вы полностью оснащены!"]
    return "\n".join(lines)


def _next_step_keyboard(j: dict) -> InlineKeyboardMarkup | None:
    step = j["next_step"]
    if step == "complete":
        return None
    if step == "diagnose":
        btn = InlineKeyboardButton(text="🧪 Пройти диагностику", callback_data="setup_step:diagnose")
    elif step == "subscribe":
        btn = InlineKeyboardButton(text="💳 Оформить подписку", url=_SUBSCRIPTION_URL)
    elif step == "browser":
        btn = InlineKeyboardButton(text="🔌 Установить расширение", callback_data="setup_step:browser")
    else:
        btn = InlineKeyboardButton(text="🐙 Подключить GitHub", callback_data="setup_step:github")
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


# ─────────────────────────────────────────────
# Guided flow messages (Ф9)
# ─────────────────────────────────────────────

def _step_text_diagnose() -> str:
    return (
        "🧪 <b>Шаг 1: Диагностика ступени</b>\n\n"
        "Покажет, с какого потока начать — не придётся гадать.\n\n"
        "Запустите /diagnose прямо здесь — займёт 2–3 минуты."
    )


def _step_text_subscribe(stage: int | None) -> str:
    if stage and stage >= 2:
        stream_hint = f"Ваша ступень {stage} — поток S{min(stage, 4)} начнётся с нужного уровня."
    else:
        stream_hint = "14 дней по 20 минут — базовый темп для начала."
    return (
        "💳 <b>Шаг 2: Подписка «Бесконечное развитие»</b>\n\n"
        f"{stream_hint}\n\n"
        "Подписка открывает личную базу знаний, браузерную среду и персональный поток."
    )


def _step_text_browser() -> str:
    return (
        "🔌 <b>Шаг 3: Браузерная среда</b>\n\n"
        "Работайте с IWE без установки приложения — прямо в браузере.\n\n"
        f"1. Откройте <a href='{_BROWSER_GUIDE_URL}'>инструкцию по установке</a>\n"
        "2. После установки выполните /connect в боте"
    )


def _step_text_github() -> str:
    return (
        "🐙 <b>Шаг 4: Репозиторий GitHub</b>\n\n"
        "Ваши знания останутся с вами — не в системе. "
        "GitHub-репозиторий — ваша личная база, независимая от платформы.\n\n"
        f"1. Откройте <a href='{_GITHUB_GUIDE_URL}'>инструкцию по настройке</a>\n"
        "2. После настройки выполните /github в боте"
    )


# ─────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────

@setup_router.message(Command("setup"))
async def cmd_setup(message: Message) -> None:
    """Показать дашборд прогресса T1→T4 (с re-entry для повторных визитов)."""
    chat_id = message.chat.id

    ory_uuid = await resolve_ory_id_from_chat(chat_id)
    if not ory_uuid:
        await message.answer(
            "Для использования /setup привяжите аккаунт Aisystant через /link."
        )
        return

    # Параллельно: тир (Railway) + диагноз (Neon) + onb_state (Neon) + intern (Railway)
    tier, cp_row, onb, intern = await asyncio.gather(
        detect_ui_tier(chat_id),
        get_latest_cp_assessment(ory_uuid),
        get_onboarding_state(ory_uuid),
        get_intern(chat_id),
    )

    j = _compute_journey(tier, cp_row, onb)

    # Ф10: re-entry — пользователь открыл /setup не сегодня
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
        if last_active and last_active < today and j["next_step"] != "complete":
            is_returning = True

    if is_returning:
        step_name = _STEP_NAMES.get(j["next_step"], j["next_step"])
        kb = _next_step_keyboard(j)
        await message.answer(
            f"С возвращением! Продолжим с <b>{step_name}</b>?",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    text = _render_dashboard(j)
    kb = _next_step_keyboard(j)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@setup_router.callback_query(F.data.startswith("setup_step:"))
async def on_setup_step(callback: CallbackQuery) -> None:
    """Guided flow: показать описание шага с «зачем» и CTA (Ф9)."""
    # double-tap protection
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    chat_id = callback.from_user.id
    step = callback.data.split(":", 1)[1]

    ory_uuid = await resolve_ory_id_from_chat(chat_id)
    stage: int | None = None

    if ory_uuid:
        # Cooldown-sync + получаем stage для персонализации текста подписки
        cp_row, _ = await asyncio.gather(
            get_latest_cp_assessment(ory_uuid),
            write_last_nudge_at(ory_uuid),
        )
        stage = cp_row.get("stage") if cp_row else None

    if step == "diagnose":
        text = _step_text_diagnose()
        kb = None  # CTA = /diagnose command, не кнопка
    elif step == "subscribe":
        text = _step_text_subscribe(stage)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💳 Оформить подписку", url=_SUBSCRIPTION_URL)
        ]])
    elif step == "browser":
        text = _step_text_browser()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Готово, выполняю /connect", callback_data="setup_browser_done")
        ]])
    elif step == "github":
        text = _step_text_github()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Готово, выполняю /github", callback_data="setup_github_done")
        ]])
    else:
        logger.warning("[Setup] unknown step: %s chat_id=%s", step, chat_id)
        await callback.message.answer("Неизвестный шаг. Попробуйте /setup снова.")
        return

    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@setup_router.callback_query(F.data.in_({"setup_browser_done", "setup_github_done"}))
async def on_setup_tool_done(callback: CallbackQuery) -> None:
    """Пользователь нажал «Готово» после инструкции — направить к команде."""
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    if callback.data == "setup_browser_done":
        await callback.message.answer(
            "Выполните /connect в этом чате — бот проверит подключение и обновит ваш путь."
        )
    else:
        await callback.message.answer(
            "Выполните /github в этом чате — бот проверит репозиторий и обновит ваш путь."
        )
