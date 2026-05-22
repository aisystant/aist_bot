"""
Setup Journey — маршрут оснащения T1→T4 (WP-349 Ф8-Ф9).

# see DP.SC.155, DP.SC.151

Команда /setup показывает дашборд прогресса: текущий тир, ступень мастерства,
чекбоксы подключённых инструментов, CTA следующего шага.

Данные читаются параллельно через asyncio.gather:
  - tier_detector (кэш 5 мин) → актуальный тир
  - cp_assessments (Neon, live) → ступень мастерства
  - onboarding_state (Neon, first_use_* флаги) → статус инструментов

Guided flow проводит по шагам. double-tap protection: callback.answer() +
edit_reply_markup(None) перед бизнес-логикой. Каждый CTA-клик пишет last_nudge_at.
"""
from __future__ import annotations

import asyncio
import logging

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

_DIAGNOSE_URL = PLATFORM_URLS.get("lr", "https://system-school.ru/programs/intro")
_SUBSCRIPTION_URL = PLATFORM_URLS.get("subscription", "https://system-school.ru/open-endedness")
_BROWSER_GUIDE_URL = "https://docs.system-school.ru/ru/iwe/browser-extension"
_GITHUB_GUIDE_URL = "https://docs.system-school.ru/ru/iwe/github-setup"


# ─────────────────────────────────────────────
# Journey logic
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
        btn = InlineKeyboardButton(
            text="🧪 Пройти диагностику",
            callback_data="setup_step:diagnose",
        )
    elif step == "subscribe":
        btn = InlineKeyboardButton(
            text="💳 Оформить подписку",
            url=_SUBSCRIPTION_URL,
        )
    elif step == "browser":
        btn = InlineKeyboardButton(
            text="🔌 Установить расширение",
            callback_data="setup_step:browser",
        )
    else:  # github
        btn = InlineKeyboardButton(
            text="🐙 Подключить GitHub",
            callback_data="setup_step:github",
        )
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


# ─────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────

@setup_router.message(Command("setup"))
async def cmd_setup(message: Message) -> None:
    """Показать дашборд прогресса T1→T4."""
    chat_id = message.chat.id
    intern = await get_intern(chat_id)
    lang = (intern or {}).get("language", "ru") or "ru"

    ory_uuid = await resolve_ory_id_from_chat(chat_id)
    if not ory_uuid:
        await message.answer(
            "Для использования /setup привяжите аккаунт Aisystant через /link."
        )
        return

    tier, cp_row, onb = await asyncio.gather(
        detect_ui_tier(chat_id),
        get_latest_cp_assessment(ory_uuid),
        get_onboarding_state(ory_uuid),
    )

    j = _compute_journey(tier, cp_row, onb)
    text = _render_dashboard(j)
    kb = _next_step_keyboard(j)

    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@setup_router.callback_query(F.data.startswith("setup_step:"))
async def on_setup_step(callback: CallbackQuery) -> None:
    """Обработать нажатие CTA guided flow."""
    # double-tap protection
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    chat_id = callback.from_user.id
    step = callback.data.split(":", 1)[1]

    ory_uuid = await resolve_ory_id_from_chat(chat_id)

    if ory_uuid:
        # Cooldown-sync: не допускаем дубль от onboarding_controller в течение 24h
        await write_last_nudge_at(ory_uuid)

    if step == "diagnose":
        await callback.message.answer(
            "Чтобы пройти диагностику ступени — запустите /diagnose прямо здесь или "
            f"перейдите в программу: <a href='{_DIAGNOSE_URL}'>Личное развитие</a>",
            parse_mode="HTML",
        )
    elif step == "browser":
        await callback.message.answer(
            "Для подключения браузер-расширения:\n"
            f"1. Откройте <a href='{_BROWSER_GUIDE_URL}'>инструкцию по установке</a>\n"
            "2. После установки выполните /connect в боте",
            parse_mode="HTML",
        )
    elif step == "github":
        await callback.message.answer(
            "Для подключения репозитория GitHub:\n"
            f"1. Откройте <a href='{_GITHUB_GUIDE_URL}'>инструкцию</a>\n"
            "2. После настройки выполните /github в боте",
            parse_mode="HTML",
        )
    else:
        logger.warning("[Setup] unknown step: %s chat_id=%s", step, chat_id)
        await callback.message.answer("Неизвестный шаг. Попробуйте /setup снова.")
