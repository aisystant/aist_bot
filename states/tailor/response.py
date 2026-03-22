"""
Стейт: Ответ на занятие Портного (WP-149, SC.020).

Вход: callback tailor_answer → dispatcher.go_to()
Выход:
  - answered → common.mode_select (ответ принят, записан в learning_history)
  - skip → common.mode_select (пропущен)

Паттерн аналогичен workshop.marathon.question:
  enter() → приглашение ответить
  handle() → текст ответа → оценка → log_event → обратная связь
"""

from typing import Optional

from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from states.base import BaseState
from i18n import t

import logging

logger = logging.getLogger(__name__)


class TailorResponseState(BaseState):
    """Стейт сбора ответа на занятие Портного."""

    name = "tailor.response"
    display_name = {
        "ru": "Ответ на занятие",
        "en": "Lesson Response",
    }
    allow_global = ["consultation"]
    keyboard_type = "reply"

    def _get_lang(self, user) -> str:
        if isinstance(user, dict):
            return user.get('language', 'ru') or 'ru'
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    async def enter(self, user, context: dict = None) -> None:
        """Приглашение ответить на вопрос занятия.

        Context содержит:
          - topic_id: str
          - bloom_depth: int
          - direction: int
          - lesson: dict (structured lesson из кэша)
        """
        lang = self._get_lang(user)
        context = context or {}

        topic_id = context.get('topic_id', '')
        bloom_depth = context.get('bloom_depth', 1)

        # Сохраняем контекст для handle()
        self._response_context = context

        # Извлечь вопрос из занятия
        lesson = context.get('lesson', {})
        question = ''
        if lesson:
            assessment = lesson.get('assessment', {})
            question = assessment.get('transfer_test', '')

        prompt_text = (
            f"✂️ <b>Портной — ответ на занятие</b>\n\n"
        )
        if question:
            prompt_text += f"❓ {question}\n\n"
        prompt_text += "Напиши свой ответ текстом."

        skip_btn = "⏭ Пропустить"
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=skip_btn)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )

        await self.send(user, prompt_text, parse_mode="HTML", reply_markup=keyboard)

    async def handle(self, user, message: Message) -> Optional[str]:
        """Обработка ответа пользователя.

        Returns:
            'answered' — ответ принят
            'skip' — пропущен
            None — остаёмся (слишком короткий)
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        context = getattr(self, '_response_context', {})

        # Пропуск
        if "пропустить" in text.lower() or "skip" in text.lower() or text == "⏭ Пропустить":
            await self._log_learning(
                chat_id=chat_id,
                context=context,
                score=0.0,
                passed=False,
                errors=[],
            )
            await self.send_remove_keyboard(
                user,
                "⏭ Занятие пропущено. Тема вернётся в следующем цикле."
            )
            return "skip"

        # Слишком короткий
        if len(text) < 15:
            await self.send(
                user,
                "Напиши ответ подробнее (минимум 15 символов)."
            )
            return None

        # Оценка через Claude Haiku
        lesson = context.get('lesson', {})
        evaluation = await self._evaluate(text, lesson)

        score = evaluation.get('score', 0.5)
        passed = evaluation.get('passed', False)
        feedback = evaluation.get('feedback', '')
        errors = evaluation.get('errors', [])

        # Запись в learning_history
        await self._log_learning(
            chat_id=chat_id,
            context=context,
            score=score,
            passed=passed,
            errors=errors,
        )

        # Обратная связь
        status_emoji = "✅" if passed else "🔄"
        response_text = f"{status_emoji} <b>Оценка:</b> {score:.0%}\n\n"
        if feedback:
            response_text += f"{feedback}\n\n"

        if passed:
            response_text += "Отличный ответ! Завтра — следующая тема."
        else:
            response_text += "Тема вернётся для закрепления."

        await self.send_remove_keyboard(user, response_text, parse_mode="HTML")
        return "answered"

    async def _evaluate(self, answer_text: str, lesson: dict) -> dict:
        """Оценить ответ через evaluator (канало-независимый)."""
        try:
            from engines.tailor.evaluator import evaluate_answer
            from bot import claude
            return await evaluate_answer(
                answer_text=answer_text,
                lesson=lesson,
                claude_client=claude,
            )
        except Exception as e:
            logger.warning(f"[Tailor/Response] Evaluation failed: {e}")
            return {
                'score': 0.5,
                'passed': False,
                'feedback': 'Спасибо за ответ! Мы его записали.',
                'errors': [],
            }

    async def _log_learning(
        self,
        chat_id: int,
        context: dict,
        score: float,
        passed: bool,
        errors: list,
    ):
        """Записать событие learning_completed → trigger → learning_history."""
        if not chat_id:
            return

        try:
            from db.queries.events import log_event

            topic_id = context.get('topic_id', '')
            bloom_depth = context.get('bloom_depth', 1)
            direction = context.get('direction', 1)
            lesson = context.get('lesson', {})
            metadata = lesson.get('metadata', {})

            await log_event(
                user_id=chat_id,
                event_type='learning_completed',
                payload={
                    'program_id': 'SS.F1',
                    'topic_id': topic_id,
                    'direction': direction,
                    'bloom_level': bloom_depth,
                    'cell_id': f"{topic_id}@{bloom_depth}",
                    'score': score,
                    'passed': passed,
                    'errors': errors,
                    'tailor_context': metadata.get('tailor_context', {}),
                    '_schema_version': 1,
                },
                source='bot',
            )
        except Exception as e:
            logger.warning(f"[Tailor/Response] log_event failed: {e}")

    async def exit(self, user) -> dict:
        """Контекст для следующего стейта."""
        return {}
