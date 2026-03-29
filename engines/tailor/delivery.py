"""
Бот-клиент Портного (WP-149, SC.020, WP-175).

Бот = тонкий клиент. Портной живёт на платформе L2 (DS-autonomous-agents/agents/tailor/).
Эта функция вызывается из scheduler бота как notify_fn:
  - Форматирует готовое занятие для Telegram
  - Отправляет пользователю с кнопками Ответить / Пропустить

Сборка + генерация = DS-autonomous-agents/agents/tailor/ (платформа).
Бот НЕ создаёт Claude-клиент для Портного — это больше не его задача.

Интеграция:
  scheduler.py вызывает notify_tailor_lesson(chat_id, user_uuid, lesson, generated)
  states/tailor/response.py обрабатывает ответ пользователя
"""

import logging
import re
from typing import Optional
from uuid import UUID

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


def _format_lesson_html(lesson: dict, generated: dict) -> str:
    """Форматировать занятие в HTML для Telegram."""
    area_name = lesson.get('area_name', '')
    impact_type = lesson.get('impact_type', 'worldview')
    element_id = lesson.get('element_id', '')
    depth = lesson.get('depth', 1)

    if impact_type == 'worldview':
        depth_names = {1: 'Осознание', 2: 'Различение', 3: 'Компиляция'}
        type_label = f"Мировоззрение · {depth_names.get(depth, '')}"
    else:
        degree_names = {1: 'Объяснение', 2: 'Умение', 3: 'Навык', 4: 'Мастерство'}
        type_label = f"Мастерство · {degree_names.get(depth, '')}"

    parts = [f"<b>{area_name}</b> · {type_label}\n<i>{element_id}</i>\n"]

    for key in ('intro', 'lesson_text'):
        val = generated.get(key, '')
        if val:
            parts.append(f"{val}\n")

    question = (
        generated.get('diagnostic_question')
        or generated.get('distinction_question')
        or generated.get('check_question')
        or generated.get('question', '')
    )
    if question:
        parts.append(f"\n<b>Вопрос:</b>\n{question}\n")

    task = (
        generated.get('compilation_task')
        or generated.get('contradiction_task')
        or generated.get('practice_task')
        or generated.get('task', '')
    )
    if task:
        parts.append(f"\n<b>Задание:</b>\n{task}\n")

    if generated.get('check_in'):
        parts.append(f"\n<b>Ежедневный чек-ин:</b>\n{generated['check_in']}\n")

    if generated.get('reflection'):
        parts.append(f"\n<b>Рефлексия:</b>\n{generated['reflection']}")

    return "\n".join(parts)

# Callback data prefixes
CB_TAILOR_ANSWER = "tailor_answer"
CB_TAILOR_SKIP = "tailor_skip"


async def notify_tailor_lesson(
    chat_id: int,
    user_uuid: UUID,
    lesson: dict,
    generated: dict,
    bot: Bot,
) -> bool:
    """Отправить готовое занятие пользователю в Telegram.

    Это notify_fn для activity-hub deliver_tailor_lesson().
    Вызывается когда платформа собрала и сгенерировала занятие.

    Args:
        chat_id: Telegram chat_id (для отправки)
        user_uuid: Ory UUID (для логирования)
        lesson: structured lesson из TailorEngine.assemble()
        generated: результат generate_lesson_text()
        bot: aiogram Bot instance

    Returns:
        True если отправлено успешно.
    """
    text = _format_lesson_html(lesson, generated)
    keyboard = _build_keyboard(lesson)

    try:
        try:
            await bot.send_message(
                chat_id,
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception:
            # Fallback: без форматирования
            clean = re.sub(r'<[^>]+>', '', text)
            await bot.send_message(chat_id, clean, reply_markup=keyboard)

        logger.info("[Tailor/Bot] Lesson sent to chat_id=%s (uuid=%s)", chat_id, user_uuid)
        return True

    except Exception as e:
        logger.error("[Tailor/Bot] Send failed for chat_id=%s: %s", chat_id, e)
        return False


def _build_keyboard(lesson: dict) -> InlineKeyboardMarkup:
    """Построить inline-клавиатуру: Ответить / Пропустить."""
    element_id = lesson.get('element_id', '')
    depth = lesson.get('depth', 1)
    impact_type = lesson.get('impact_type', 'worldview')

    # Формат: tailor_answer:{element_id}:{depth}:{impact_type}
    answer_data = f"{CB_TAILOR_ANSWER}:{element_id}:{depth}:{impact_type}"
    skip_data = f"{CB_TAILOR_SKIP}:{element_id}:{depth}:{impact_type}"

    # Обрезать до 64 байт (Telegram ограничение)
    answer_data = answer_data[:64]
    skip_data = skip_data[:64]

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Ответить", callback_data=answer_data),
            InlineKeyboardButton(text="⏭ Пропустить", callback_data=skip_data),
        ]
    ])
