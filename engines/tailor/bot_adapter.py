"""
Telegram-адаптер для Портного (WP-149, SC.020).

Реализует TailorPort для канала Telegram Bot.
Канало-зависимое: HTML-форматирование, InlineKeyboard, Telegram API.
"""

import logging
import re
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from engines.tailor.port import LessonDeliveryResult, TailorPort
from helpers.message_split import split_message_safe

logger = logging.getLogger(__name__)

# Callback data prefixes
CB_TAILOR_ANSWER = "tailor_answer"
CB_TAILOR_SKIP = "tailor_skip"


class BotTailorAdapter(TailorPort):
    """Telegram Bot реализация TailorPort."""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def deliver(
        self,
        user_id: int,
        lesson: dict,
        generated: dict,
    ) -> LessonDeliveryResult:
        """Доставить занятие через Telegram.

        Длинные уроки (>4000 символов) разбиваются на части. Keyboard
        прикрепляется только к последней части, иначе кнопки появятся
        под промежуточным сообщением.
        """
        text = await self.format_lesson(lesson, generated)
        keyboard = self._build_keyboard(lesson)
        parts = split_message_safe(text)
        last_idx = len(parts) - 1

        try:
            last_msg = None
            for i, part in enumerate(parts):
                rm = keyboard if i == last_idx else None
                try:
                    last_msg = await self.bot.send_message(
                        user_id,
                        part,
                        parse_mode="HTML",
                        reply_markup=rm,
                    )
                except Exception:
                    # Fallback: без форматирования
                    clean = re.sub(r'<[^>]+>', '', part)
                    last_msg = await self.bot.send_message(
                        user_id,
                        clean,
                        reply_markup=rm,
                    )

            logger.info(
                f"[Tailor/Bot] Delivered lesson to {user_id} "
                f"(parts={len(parts)}, total_len={len(text)})"
            )
            return LessonDeliveryResult(
                delivered=True,
                message_id=last_msg.message_id if last_msg else 0,
            )

        except Exception as e:
            logger.error(f"[Tailor/Bot] Delivery failed for {user_id}: {e}")
            return LessonDeliveryResult(
                delivered=False,
                error=str(e),
            )

    async def format_lesson(self, lesson: dict, generated: dict) -> str:
        """Форматировать занятие в HTML для Telegram."""
        area_name = lesson.get('area_name', '')
        impact_type = lesson.get('impact_type', 'worldview')
        depth = lesson.get('depth', 1)

        if impact_type == 'worldview':
            depth_label = {1: 'Осознание', 2: 'Различение', 3: 'Компиляция'}.get(depth, '')
            type_label = f"Мировоззрение · {depth_label}"
        else:
            depth_label = {1: 'Объяснение', 2: 'Умение', 3: 'Навык', 4: 'Мастерство'}.get(depth, '')
            type_label = f"Мастерство · {depth_label}"

        intro = generated.get('intro', '')
        text = generated.get('lesson_text', '')
        question = generated.get('question', '')
        task = generated.get('task', '')
        reflection = generated.get('reflection', '')

        parts = []

        # Заголовок
        parts.append(
            f"✂️ <b>{area_name}</b>\n"
            f"<i>{type_label}</i>\n"
        )

        if intro:
            parts.append(f"{intro}\n")

        if text:
            parts.append(f"{text}\n")

        if question:
            parts.append(f"\n❓ <b>Вопрос:</b>\n{question}\n")

        if task:
            parts.append(f"\n📝 <b>Задание:</b>\n{task}\n")

        if reflection:
            parts.append(f"\n💭 <b>Рефлексия:</b>\n{reflection}")

        return "\n".join(parts)

    def _build_keyboard(self, lesson: dict) -> InlineKeyboardMarkup:
        """Построить inline-клавиатуру для занятия."""
        element_id = lesson.get('element_id', '')
        depth = lesson.get('depth', 1)
        area = lesson.get('area', 1)

        # Encode контекст в callback_data (max 64 bytes)
        # Формат: tailor_answer:{element_id}:{depth}:{area}
        answer_data = f"{CB_TAILOR_ANSWER}:{element_id}:{depth}:{area}"
        skip_data = f"{CB_TAILOR_SKIP}:{element_id}:{depth}:{area}"

        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Ответить",
                    callback_data=answer_data,
                ),
                InlineKeyboardButton(
                    text="⏭ Пропустить",
                    callback_data=skip_data,
                ),
            ]
        ])
