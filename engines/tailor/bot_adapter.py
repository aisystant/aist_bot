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
from engines.tailor.planner import BLOOM_NAMES, DIRECTION_NAMES

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
        """Доставить занятие через Telegram."""
        text = await self.format_lesson(lesson, generated)
        keyboard = self._build_keyboard(lesson)

        try:
            try:
                msg = await self.bot.send_message(
                    user_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except Exception:
                # Fallback: без форматирования
                clean = re.sub(r'<[^>]+>', '', text)
                msg = await self.bot.send_message(
                    user_id,
                    clean,
                    reply_markup=keyboard,
                )

            logger.info(f"[Tailor/Bot] Delivered lesson to {user_id}")
            return LessonDeliveryResult(
                delivered=True,
                message_id=msg.message_id,
            )

        except Exception as e:
            logger.error(f"[Tailor/Bot] Delivery failed for {user_id}: {e}")
            return LessonDeliveryResult(
                delivered=False,
                error=str(e),
            )

    async def format_lesson(self, lesson: dict, generated: dict) -> str:
        """Форматировать занятие в HTML для Telegram."""
        topic_name = lesson.get('topic_name', '')
        direction = DIRECTION_NAMES.get(lesson.get('direction', 0), '')
        bloom_depth = lesson.get('bloom_depth', 1)

        intro = generated.get('intro', '')
        text = generated.get('lesson_text', '')
        question = generated.get('question', '')
        task = generated.get('task', '')
        reflection = generated.get('reflection', '')

        parts = []

        # Заголовок
        parts.append(
            f"✂️ <b>{topic_name}</b>\n"
            f"<i>{direction} · Глубина {bloom_depth}</i>\n"
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
        topic_id = lesson.get('topic_id', '')
        bloom_depth = lesson.get('bloom_depth', 1)
        direction = lesson.get('direction', 1)

        # Encode контекст в callback_data (max 64 bytes)
        # Формат: tailor_answer:{topic_id}:{bloom}:{dir}
        answer_data = f"{CB_TAILOR_ANSWER}:{topic_id}:{bloom_depth}:{direction}"
        skip_data = f"{CB_TAILOR_SKIP}:{topic_id}:{bloom_depth}:{direction}"

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
