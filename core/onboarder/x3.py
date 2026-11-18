"""
Х3 — выбор траектории: делегация Диагносту + оффер курса (WP-406 Ф5).

# see DP.SC.170, DP.ROLE.067

Х3 закрывается тремя частями (карточка WP-406, критерий строки 123):
  Х3.1 ступень 1-5      — готово: Диагност R28 (handlers/diagnose.py).
  Х3.2 узкое место      — готово: Диагност возвращает bottleneck_slot.
  Х3.3 выбор курса      — Онбордер транслирует recommended_stream в оффер курса.

`recommended_stream` уже в проде (db/queries/cp_assessment.py:compute_cp_stage):
  "РР"  — для ступени 5 (Проактивный → программа «Рабочее развитие», WP-371);
  "S1".."S4" — поток развития по ступени.

Архитектура (консенсус peer-сессии 2026-06-11-13-wp406-f5-x3-impl):
  - Fast path: get_latest_cp_assessment → рекомендованный поток → _show_x3_offer.
  - Bridge path: нет cp-среза → сохранить return_to с TTL → предложить /diagnose.
    После завершения Диагноста _finish_diagnose вызывает check_x3_return_to_bridge,
    который находит return_to и вызывает _show_x3_offer.
  - mark_x3_done ставится только при явном подтверждении (✅ от пользователя).
  - _show_x3_offer и check_x3_return_to_bridge живут здесь, чтобы handlers/diagnose.py
    мог импортировать без циклического импорта (core → handlers запрещён).
"""

import asyncio
import datetime
import html
import logging
import math
from typing import Optional

from core.qualification import QUALIFIED_NUMERIC_MIN
from db.queries.cp_assessment import (
    PROGRAM_STAGE_LABELS,
    program_stage_for_stored_stream,
    qualification_programme,
)

logger = logging.getLogger(__name__)


_RETURN_TO_X3 = "x3_offer"
_RETURN_TO_TTL_SECONDS = 3600  # мост устаревает через 1 час если diagnose не завершён

# Х3 for «Работник» and above (WP-417 Ф8, pilot 18.11.2026, option 2A + reading rule A): the
# offer follows the qualification, as the /diagnose result does (WP-385 Ф18). Stream codes in the
# x3_confirm callback and the x3_completed event for these offers:
QUALIFICATION_STREAM = "QUAL"   # programme by qualification (qualification_programme)
MENTOR_STREAM = "MENTOR"        # qualification has no known programme — a mentor picks the path

QUALIFIED, LEARNER, UNKNOWN = "qualified", "learner", "unknown"
# Same ceiling as the qualification read of /diagnose (handlers/diagnose.py).
QUALIFICATION_READ_SECONDS = 8
# Gateway codes meaning the twin login is gone, not a passing failure (clients/gateway_mcp.py).
_NO_LOGIN_ERRORS = frozenset({"not_authorized", "token_expired"})


def describe_stream(recommended_stream: str) -> str:
    """Stored stream code → the programme stage a learner is offered, readable for the learner.

    Stored codes 'S1'..'S4' equal the stage, 'РР' is the legacy code of stage 5; the offer
    follows the admission table (WP-385 Ф18.2). An unknown code is returned as is.
    """
    if recommended_stream == QUALIFICATION_STREAM:
        return "программа по квалификации"
    if recommended_stream == MENTOR_STREAM:
        return "подбор программы с наставником"
    code = program_stage_for_stored_stream(recommended_stream)
    if code is None:
        return recommended_stream
    if code == "РР":
        return PROGRAM_STAGE_LABELS[code]
    return f"этап {PROGRAM_STAGE_LABELS[code]} программы «Личное развитие»"


async def read_onboarding_qualification(chat_id: int) -> tuple[str, Optional[str]]:
    """(QUALIFIED, name) | (LEARNER, None) | (UNKNOWN, None) for the Х3 offer (pilot 18.11.2026, rule A).

    LEARNER: no twin connected (including a lost or expired login — a retry cannot fix it), the
    twin has no qualification, or it is below «Работник» — the onboarding assigns «Ученик» to
    people without a qualification anyway (WP-406 Ф31).
    UNKNOWN: the twin is connected but the read failed twice, timed out (no retry: the wait
    already includes the gateway queue), or the record has no usable level number — then the
    offer must not pick a learner stage.
    `asyncio.timeout`, not `wait_for`: the gateway reports errors per task, and on Python 3.11
    `wait_for` runs the call in another task.
    """
    from clients.gateway_mcp import gateway_mcp

    if not gateway_mcp.is_connected(chat_id):
        return LEARNER, None
    for attempt in (1, 2):
        try:
            async with asyncio.timeout(QUALIFICATION_READ_SECONDS):
                degree = await gateway_mcp.dt_read_degree(chat_id)
        except TimeoutError:
            logger.warning("[x3] qualification read timed out (chat=%s, attempt %s)", chat_id, attempt)
            return UNKNOWN, None
        except Exception as e:
            logger.warning("[x3] qualification read failed (chat=%s, attempt %s): %r", chat_id, attempt, e)
            continue
        if degree is None:
            if gateway_mcp.last_call_error == "ok":
                return LEARNER, None
            if gateway_mcp.last_call_error in _NO_LOGIN_ERRORS:
                return LEARNER, None
            logger.warning("[x3] qualification read failed (chat=%s, attempt %s): %s",
                           chat_id, attempt, gateway_mcp.last_call_error)
            continue
        numeric = _level_number(degree)
        if numeric is None:
            logger.warning("[x3] qualification record without a level number (chat=%s): %r", chat_id, degree)
            return UNKNOWN, None
        return (QUALIFIED, degree["level"]) if numeric >= QUALIFIED_NUMERIC_MIN else (LEARNER, None)
    return UNKNOWN, None


def _level_number(degree: dict) -> Optional[float]:
    try:
        numeric = float(degree.get("numeric"))
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


async def _show_qualified_offer(bot, chat_id: int, qualification: str, from_bridge: bool) -> None:
    """Offer for «Работник» and above: the programme by qualification, or a mentor when it has none."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    programme = qualification_programme(qualification)
    qualification = html.escape(qualification)
    if programme:
        stream = QUALIFICATION_STREAM
        text = (f"🎓 Твоя квалификация — <b>{qualification}</b>.\n\n"
                f"Тебе подходит <b>{programme}</b>.\n\nХочешь начать?")
    else:
        stream = MENTOR_STREAM
        text = (f"🎓 Твоя квалификация — <b>{qualification}</b>.\n\n"
                "Программу под неё подберёт наставник.\n\nПродолжим?")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, начинаю", callback_data=f"x3_confirm:{stream}:{int(from_bridge)}"),
    ]])
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)


async def _show_neutral_offer(bot, chat_id: int) -> None:
    """Twin connected, qualification unreadable: no stage is picked; /diagnose returns here via the bridge."""
    from core.onboarder import storage

    await storage.save_onboarding_context(chat_id, {
        "return_to": _RETURN_TO_X3,
        "set_at": datetime.datetime.utcnow().isoformat(),
    })
    await bot.send_message(
        chat_id,
        "Подберём тебе программу. Сейчас не получилось прочитать твою квалификацию, "
        "поэтому этап не предлагаю.\n\n"
        "Пройди диагностику: /diagnose — или напиши наставнику.",
    )


async def run_x3(intern: dict, message) -> None:
    """Полный срез Х3: fast path (cp_assessment) или bridge path (через Диагноста).

    Fast path: cp_assessment уже есть → сразу предложить курс (_show_x3_offer).
    Bridge path: нет среза → сохранить return_to с TTL → предложить /diagnose.
      Когда Диагност завершит (_finish_diagnose), check_x3_return_to_bridge найдёт
      return_to и вызовет _show_x3_offer с from_bridge=True.
    """
    from helpers.dual_write import resolve_ory_id_from_chat
    from db.queries.cp_assessment import get_latest_cp_assessment
    from core.onboarder import storage

    chat_id = intern.get("chat_id") or message.chat.id
    lang = intern.get("language", "ru") or "ru"

    status, qualification = await read_onboarding_qualification(chat_id)
    if status == QUALIFIED:
        await _show_qualified_offer(message.bot, chat_id, qualification, from_bridge=False)
        return
    if status == UNKNOWN:
        await _show_neutral_offer(message.bot, chat_id)
        return

    account_id = await resolve_ory_id_from_chat(chat_id)
    if not account_id:
        account_id = intern.get("dt_user_id")

    cp = await get_latest_cp_assessment(account_id) if account_id else None

    if cp:
        await _show_x3_offer(
            message.bot, chat_id,
            cp["recommended_stream"], cp.get("bottleneck_slot"),
            lang=lang, from_bridge=False,
        )
    else:
        await storage.save_onboarding_context(chat_id, {
            "return_to": _RETURN_TO_X3,
            "set_at": datetime.datetime.utcnow().isoformat(),
        })
        await message.answer(
            "Чтобы понять, с чего тебе начать работать, нужно 3–5 вопросов.\n\n"
            "Запусти диагностику: /diagnose, и я подберу подходящий формат.",
        )


async def _show_x3_offer(
    bot,
    chat_id: int,
    recommended_stream: str,
    bottleneck_slot: Optional[str],
    lang: str = "ru",
    from_bridge: bool = False,
) -> None:
    """Показать оффер курса после определения рекомендованного потока.

    Чистая функция: импортируется и из handlers/onboarding.py, и из handlers/diagnose.py
    без циклического импорта (core не импортирует handlers).

    from_bridge=True: пользователь только что прошёл Диагноста — кнопка «уточнить» лишняя.
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    from db.queries.cp_assessment import describe_bottleneck

    stream_name = describe_stream(recommended_stream)
    # bottleneck_slot хранится как строка "none" когда узкого места нет (NOT NULL в БД)
    effective_bottleneck = bottleneck_slot if bottleneck_slot and bottleneck_slot != "none" else None
    bottleneck_text = f" (узкое место: <b>{describe_bottleneck(effective_bottleneck)}</b>)" if effective_bottleneck else ""

    text = (
        f"🎯 Тебе подходит <b>{stream_name}</b>{bottleneck_text}.\n\n"
        "Хочешь начать?"
    )

    if from_bridge:
        # After Diagnostician — third field :1 encodes diagnostic_done=True for x3_completed event
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Да, начинаю",
                callback_data=f"x3_confirm:{recommended_stream}:1",
            ),
        ]])
    else:
        # Fast path — third field :0 encodes diagnostic_done=False for x3_completed event
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Да, начинаю",
                callback_data=f"x3_confirm:{recommended_stream}:0",
            ),
            InlineKeyboardButton(
                text="🔍 Уточнить через диагностику",
                callback_data="start_diagnose_for_x3",
            ),
        ]])

    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)


async def check_x3_return_to_bridge(bot, chat_id: int, profile: dict) -> None:
    """Проверить мост return_to после завершения Диагноста.

    Вызывается из handlers/diagnose.py._finish_diagnose (нет циклического импорта).
    Если return_to == "x3_offer" и TTL не истёк → показать оффер курса и сбросить мост.
    """
    from core.onboarder import storage

    try:
        onb_ctx = await storage.get_onboarding_context(chat_id)
    except Exception as e:
        logger.debug("[x3_bridge] get_onboarding_context failed: %s", e)
        return

    if onb_ctx.get("return_to") != _RETURN_TO_X3:
        return

    set_at_str = onb_ctx.get("set_at")
    if not set_at_str:
        return

    try:
        dt = datetime.datetime.fromisoformat(set_at_str)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        age = (datetime.datetime.utcnow() - dt).total_seconds()
    except (ValueError, TypeError):
        logger.debug("[x3_bridge] invalid set_at: %s", set_at_str)
        return

    if age >= _RETURN_TO_TTL_SECONDS:
        logger.debug("[x3_bridge] TTL expired (age=%.0fs), clearing stale return_to", age)
        await storage.save_onboarding_context(chat_id, {"return_to": None, "set_at": None})
        return

    status, qualification = await read_onboarding_qualification(chat_id)
    if status != LEARNER:
        await storage.save_onboarding_context(chat_id, {"return_to": None, "set_at": None})
        if status == QUALIFIED:
            await _show_qualified_offer(bot, chat_id, qualification, from_bridge=True)
        else:
            await bot.send_message(
                chat_id, "Квалификацию прочитать не получилось, этап не предлагаю — напиши наставнику, "
                         "он подберёт программу.")
        return

    stream = profile.get("recommended_stream", "")
    if not stream:
        logger.warning("[x3_bridge] profile missing recommended_stream for chat_id=%s", chat_id)
        return
    await _show_x3_offer(
        bot, chat_id,
        stream, profile.get("bottleneck_slot"),
        from_bridge=True,
    )
    await storage.save_onboarding_context(chat_id, {"return_to": None, "set_at": None})
