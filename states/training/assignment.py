"""
Стейт: Задание тренировки.

Вход: из training.dashboard (пользователь выбрал принцип)
Генерирует задание, принимает ответ, оценивает через AI.
"""

from typing import Optional, Dict

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from states.base import BaseState
from engines.training.engine import TrainingEngine
from config import get_logger, TRAINING_MIN_ANSWER_LENGTH, TRAINING_MAX_DEPTH

logger = get_logger(__name__)


class TrainingAssignmentState(BaseState):
    """Задание + ожидание ответа + оценка."""

    name = "training.assignment"
    display_name = {
        "ru": "Задание тренировки",
        "en": "Training Assignment",
    }
    allow_global = ["consultation", "notes"]

    _user_data: Dict[int, Dict] = {}

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    async def enter(self, user, context: dict = None) -> Optional[str]:
        chat_id = self._get_chat_id(user)
        principle_id = (context or {}).get('principle_id')

        if not principle_id:
            return "back"

        engine = TrainingEngine(chat_id)
        assignment = await engine.generate_assignment(principle_id)

        if not assignment:
            await self.send(user, "Этот принцип полностью пройден или недоступен.")
            return "back"

        # Сохранить данные задания
        self._user_data[chat_id] = {
            'principle_id': principle_id,
            'depth': assignment['depth'],
            'assignment_text': assignment['assignment_text'],
        }

        lines = [
            f"📝 *{assignment['principle_name']}* — Глубина {assignment['depth']} ({assignment.get('bloom_level', '')})",
        ]
        if assignment.get('bridge_text'):
            lines.append(f"\n💡 {assignment['bridge_text']}")
        lines.append(f"\n{assignment['assignment_text']}")
        lines.append("\n✏️ Напишите ваш ответ:")

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="← Назад", callback_data="train_back")
        ]])

        await self.send(user, '\n'.join(lines), reply_markup=keyboard, parse_mode="Markdown")
        return None

    async def handle(self, user, message: Message) -> Optional[str]:
        chat_id = self._get_chat_id(user)
        answer_text = (message.text or '').strip()

        if len(answer_text) < TRAINING_MIN_ANSWER_LENGTH:
            await self.send(
                user,
                f"Ответ слишком короткий (минимум {TRAINING_MIN_ANSWER_LENGTH} символов)."
            )
            return None

        data = self._user_data.get(chat_id, {})
        principle_id = data.get('principle_id')
        depth = data.get('depth')
        assignment_text = data.get('assignment_text', '')

        if not principle_id or not depth:
            return "back"

        await self.send(user, "🔍 Оцениваю ответ...")

        engine = TrainingEngine(chat_id)
        result = await engine.evaluate_answer(
            principle_id, depth, assignment_text, answer_text
        )

        if result.get('passed'):
            new_depth = result.get('new_depth', depth)
            if new_depth >= TRAINING_MAX_DEPTH:
                text = f"🎉 *Принцип пройден полностью!*\n\nГлубина {new_depth}/{TRAINING_MAX_DEPTH}"
            else:
                text = f"✅ *Принято!* Глубина {new_depth}/{TRAINING_MAX_DEPTH}"
            if result.get('feedback'):
                text += f"\n\n{result['feedback']}"
            await self.send(user, text, parse_mode="Markdown")
            return "passed"
        elif result.get('partial'):
            text = f"🔶 *Частично верно*\n\n{result.get('feedback', '')}"
            buttons = [
                [InlineKeyboardButton(text="🔄 Попробовать ещё", callback_data="train_retry")],
                [InlineKeyboardButton(text="← К дашборду", callback_data="train_back")],
            ]
            await self.send(user, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
            return None
        else:
            text = f"❌ *Не совсем*\n\n{result.get('feedback', '')}"
            buttons = [
                [InlineKeyboardButton(text="🔄 Попробовать ещё", callback_data="train_retry")],
                [InlineKeyboardButton(text="← К дашборду", callback_data="train_back")],
            ]
            await self.send(user, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
            return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        data = callback.data or ''
        if data == "train_back":
            await callback.answer()
            return "back"
        if data == "train_retry":
            await callback.answer()
            return "retry"
        return None

    async def exit(self, user) -> dict:
        chat_id = self._get_chat_id(user)
        data = self._user_data.pop(chat_id, {})
        return data
