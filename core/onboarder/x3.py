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

import datetime
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Человекочитаемые имена потоков. Источник семантики — compute_cp_stage
# (cp_assessment.py): "РР" = «Рабочее развитие» (WP-371), "S{n}" = поток ступени n.
_WORK_DEVELOPMENT_STREAM = "РР"

_RETURN_TO_X3 = "x3_offer"
_RETURN_TO_TTL_SECONDS = 3600  # мост устаревает через 1 час если diagnose не завершён


def describe_stream(recommended_stream: str) -> str:
    """Перевести код рекомендованного потока в человекочитаемое имя.

    Args:
        recommended_stream: значение из compute_cp_stage ("РР" | "S1".."S4").
    Returns:
        Строка для показа пилоту. Неизвестный код возвращается как есть
        (не выдумываем имя, см. правило «не изобретать имена артефактов»).
    """
    if recommended_stream == _WORK_DEVELOPMENT_STREAM:
        return "программа «Рабочее развитие»"
    if recommended_stream.startswith("S") and recommended_stream[1:].isdigit():
        return f"поток личного развития ступени {recommended_stream[1:]} ({recommended_stream})"
    return recommended_stream


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

    stream_name = describe_stream(recommended_stream)
    # bottleneck_slot хранится как строка "none" когда узкого места нет (NOT NULL в БД)
    effective_bottleneck = bottleneck_slot if bottleneck_slot and bottleneck_slot != "none" else None
    bottleneck_text = f" (узкое место: <b>{effective_bottleneck}</b>)" if effective_bottleneck else ""

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
