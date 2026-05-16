"""
/simulator — Лаборатория симуляций bh-характеристик (WP-319 Ф6).

# see DP.SC.133, DP.ROLE.043

Вид C (Микро): один экран + inline-кнопки.
Минимальный FSM только для «Что если» (захват текста).
"""
from __future__ import annotations

import html
import logging
import os
import sys

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from helpers.typing_indicator import keep_typing

logger = logging.getLogger(__name__)
simulator_router = Router(name="simulator")

CB_PREFIX = "sim"
WEB_URL = "https://simulator.aisystant.com"

STAGE_NAMES = {
    1: "Случайный",
    2: "Практикующий",
    3: "Систематический",
    4: "Дисциплинированный",
    5: "Проактивный",
}


# ── FSM ───────────────────────────────────────────────────────────────────────

class SimulatorStates(StatesGroup):
    waiting_whatif = State()


# ── Engine import helpers ─────────────────────────────────────────────────────

def _get_engine_path() -> str | None:
    """Путь к activity-hub: из env или относительный (локальная разработка)."""
    env_path = os.environ.get("ACTIVITY_HUB_PATH")
    if env_path:
        return env_path
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.normpath(os.path.join(here, "..", "..", "activity-hub"))
    if os.path.isdir(candidate):
        return candidate
    return None


def _ensure_engine() -> bool:
    """Добавить engine к sys.path если нужно. Возвращает True при успехе."""
    path = _get_engine_path()
    if path and path not in sys.path:
        sys.path.insert(0, path)
    try:
        import activity_hub  # noqa: F401
        return True
    except ImportError:
        return False


# ── Simulator run ─────────────────────────────────────────────────────────────

def _run_s1(profile, overrides: dict | None = None, horizon: int = 12) -> str:
    """Запустить S1 и вернуть pilot_text (или сообщение об ошибке)."""
    try:
        from activity_hub.engines.simulator.scenarios import ALL_SCENARIOS
        scenario = ALL_SCENARIOS["s1"]
        params = overrides or {}
        result = scenario.run(profile, params, horizon_weeks=horizon)
        return result.pilot_text or "Симуляция завершена — открой веб-версию для деталей."
    except Exception as e:
        logger.warning("[simulator] _run_s1 error: %s", e)
        return "Не удалось запустить симуляцию. Попробуй веб-версию:"


async def _load_real_profile(account_id: str) -> "SimulatorProfile | None":
    """Загрузить реальный bh-профиль из Neon через learning-pool."""
    try:
        from db.connection import get_learning_pool
        from activity_hub.engines.simulator.data import load_profile
        pool = await get_learning_pool()
        async with pool.acquire() as conn:
            return await load_profile(account_id, conn)
    except Exception as e:
        logger.warning("[simulator] _load_real_profile error: %s", e)
        return None


# ── UI helpers ────────────────────────────────────────────────────────────────

def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Ступень 1", callback_data=f"{CB_PREFIX}_preset_1"),
            InlineKeyboardButton(text="Ступень 2", callback_data=f"{CB_PREFIX}_preset_2"),
            InlineKeyboardButton(text="Ступень 3", callback_data=f"{CB_PREFIX}_preset_3"),
        ],
        [
            InlineKeyboardButton(text="Ступень 4", callback_data=f"{CB_PREFIX}_preset_4"),
            InlineKeyboardButton(text="Ступень 5", callback_data=f"{CB_PREFIX}_preset_5"),
        ],
        [InlineKeyboardButton(text="Мой профиль", callback_data=f"{CB_PREFIX}_profile")],
        [InlineKeyboardButton(text="Что если...", callback_data=f"{CB_PREFIX}_whatif")],
    ])


def _result_text(pilot_text: str, stage_hint: str = "") -> str:
    lines = []
    if stage_hint:
        lines.append(f"<b>{stage_hint}</b>\n")
    lines.append(pilot_text)
    lines.append(f'\n🔬 <a href="{WEB_URL}">Веб-версия</a> — графики и слайдеры')
    return "\n".join(lines)


def _make_preset_profile_safe(stage: int):
    """Создать preset профиль с защитой от ImportError подмодуля."""
    from activity_hub.engines.simulator.data import make_preset_profile
    return make_preset_profile(stage)


# ── Handlers ──────────────────────────────────────────────────────────────────

@simulator_router.message(Command("simulator"))
async def cmd_simulator(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not _ensure_engine():
        await message.answer(
            "Симулятор временно недоступен.\n"
            f'<a href="{WEB_URL}">Открыть веб-версию</a>',
            parse_mode="HTML",
        )
        return

    await message.answer(
        "🧪 <b>Симулятор развития</b>\n\n"
        "Покажу, как изменятся ваши характеристики в зависимости от интенсивности занятий.\n\n"
        "Выберите отправную точку:",
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


@simulator_router.callback_query(F.data.startswith(f"{CB_PREFIX}_preset_"))
async def on_sim_preset(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()  # сброс waiting_whatif если был активен

    try:
        stage = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        stage = 1

    if not _ensure_engine():
        await callback.message.answer(
            f'Симулятор недоступен. <a href="{WEB_URL}">Веб-версия</a>',
            parse_mode="HTML",
        )
        return

    try:
        profile = _make_preset_profile_safe(stage)
    except ImportError as e:
        logger.warning("[simulator] engine submodule import error: %s", e)
        await callback.message.answer(
            f'Симулятор временно недоступен. <a href="{WEB_URL}">Веб-версия</a>',
            parse_mode="HTML",
        )
        return

    pilot_text = _run_s1(profile)
    stage_name = STAGE_NAMES.get(stage, f"Ступень {stage}")
    await callback.message.answer(
        _result_text(pilot_text, f"Типовой профиль — {stage_name}"),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@simulator_router.callback_query(F.data == f"{CB_PREFIX}_profile")
async def on_sim_profile(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()  # сброс waiting_whatif если был активен

    if not _ensure_engine():
        await callback.message.answer(
            f'Симулятор недоступен. <a href="{WEB_URL}">Веб-версия</a>',
            parse_mode="HTML",
        )
        return

    from helpers.dual_write import resolve_ory_id_from_chat
    from db.queries import get_intern
    chat_id = callback.message.chat.id
    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        intern = await get_intern(chat_id)
        if intern:
            account_id = intern.get("dt_user_id")

    if not account_id:
        await callback.message.answer(
            "Профиль недоступен — нужна привязка к аккаунту.\n"
            f'<a href="{WEB_URL}">Веб-версия</a>',
            parse_mode="HTML",
        )
        return

    profile = await _load_real_profile(account_id)
    if profile is None or profile.source == "no_data":
        try:
            profile = _make_preset_profile_safe(1)
        except ImportError as e:
            logger.warning("[simulator] engine submodule import error: %s", e)
            await callback.message.answer(
                f'Симулятор временно недоступен. <a href="{WEB_URL}">Веб-версия</a>',
                parse_mode="HTML",
            )
            return
        hint = "Данных пока нет — показываю профиль ступени 1"
    else:
        hint = "Ваш профиль"

    pilot_text = _run_s1(profile)
    await callback.message.answer(
        _result_text(pilot_text, hint),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@simulator_router.callback_query(F.data == f"{CB_PREFIX}_whatif")
async def on_sim_whatif(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SimulatorStates.waiting_whatif)
    await callback.message.answer(
        "Напишите, что хотите изменить.\n\n"
        "<i>Например: «Хочу заниматься 8 часов в неделю и не пропускать больше 2 дней подряд»</i>",
        parse_mode="HTML",
    )


@simulator_router.message(SimulatorStates.waiting_whatif)
async def on_sim_whatif_text(message: Message, state: FSMContext) -> None:
    await state.clear()

    if not _ensure_engine():
        await message.answer(
            f'Симулятор недоступен. <a href="{WEB_URL}">Веб-версия</a>',
            parse_mode="HTML",
        )
        return

    text = message.text or ""
    if len(text) > 1000:
        text = text[:1000]

    from helpers.dual_write import resolve_ory_id_from_chat
    from db.queries import get_intern
    chat_id = message.chat.id
    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        intern = await get_intern(chat_id)
        if intern:
            account_id = intern.get("dt_user_id")

    if account_id:
        profile = await _load_real_profile(account_id)
    else:
        profile = None

    if profile is None or profile.source == "no_data":
        try:
            profile = _make_preset_profile_safe(1)
        except ImportError as e:
            logger.warning("[simulator] engine submodule import error: %s", e)
            await message.answer(
                f'Симулятор временно недоступен. <a href="{WEB_URL}">Веб-версия</a>',
                parse_mode="HTML",
            )
            return

    # LLM-парсинг сценария (~2-4 сек — показываем typing)
    async with keep_typing(message):
        try:
            from activity_hub.engines.simulator.llm_parser import parse_scenario_text
            parsed = await parse_scenario_text(text)
        except Exception as e:
            logger.warning("[simulator] llm_parser error: %s", e)
            parsed = {"scenario_id": "s1", "bh_overrides": {}, "horizon_weeks": 12,
                      "confidence": 0.0, "fallback_sliders": True, "explanation": ""}

    overrides = parsed.get("bh_overrides") or {}
    horizon = int(parsed.get("horizon_weeks") or 12)
    # W-3: экранируем LLM-текст перед вставкой в HTML-разметку
    explanation = html.escape(parsed.get("explanation") or "")

    pilot_text = _run_s1(profile, overrides=overrides, horizon=horizon)

    header = "Симуляция вашего сценария"
    if explanation:
        header += f"\n<i>{explanation}</i>"

    await message.answer(
        _result_text(pilot_text, header),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
