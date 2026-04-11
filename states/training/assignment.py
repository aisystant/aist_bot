"""
Стейт: Задание тренировки.

Вход: из training.dashboard (пользователь выбрал принцип)
Генерирует задание, принимает ответ, оценивает через AI.
"""

from typing import Optional

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from states.base import BaseState
from engines.training.engine import TrainingEngine
from engines.training.planner import get_principle_name, load_zp_cells, generate_example_answer
from db.queries.training import get_principle_depth
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

    async def enter(self, user, context: dict = None) -> Optional[str]:
        chat_id = self._get_chat_id_from_user(user)
        principle_id = (context or {}).get('principle_id')

        if not principle_id:
            return "back"

        engine = TrainingEngine(chat_id)

        try:
            assignment = await engine.generate_assignment(principle_id)
        except Exception as e:
            logger.error(f"[Training] generate_assignment error for {principle_id}: {e}")
            assignment = None

        if not assignment:
            # Различить: пройден полностью vs ошибка загрузки
            p_name = get_principle_name(principle_id)
            depth = await get_principle_depth(chat_id, principle_id)
            if depth >= TRAINING_MAX_DEPTH:
                error_text = f"✅ {p_name} — пройден полностью ({depth}/{TRAINING_MAX_DEPTH})."
            else:
                error_text = f"⚠️ Не удалось загрузить задание для {p_name}. Попробуйте позже."
                logger.error(f"[Training] No assignment for {principle_id}, depth={depth}")
            await self.send(user, error_text)
            return "back"

        # Сохранить данные задания в БД (переживает Railway redeploy)
        await self.save_state(user, {
            'principle_id': principle_id,
            'depth': assignment['depth'],
            'assignment_text': assignment['assignment_text'],
        })

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
        answer_text = (message.text or '').strip()

        if len(answer_text) < TRAINING_MIN_ANSWER_LENGTH:
            await self.send(
                user,
                f"Ответ слишком короткий (минимум {TRAINING_MIN_ANSWER_LENGTH} символов)."
            )
            return None

        data = await self.load_state(user)
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
        else:
            # Подсчёт неудач подряд (персистентный через БД)
            fail_count = data.get('fail_count', 0) + 1
            await self.save_state(user, {**data, 'fail_count': fail_count})

            if result.get('partial'):
                text = f"🔶 *Частично верно*\n\n{result.get('feedback', '')}"
            else:
                text = f"❌ *Не совсем*\n\n{result.get('feedback', '')}"

            # После 2 неудач — показать пример правильного ответа
            if fail_count >= 2:
                try:
                    cells = load_zp_cells()
                    cell_data = cells.get(principle_id, {}).get('depths', {}).get(str(depth), {})
                    p_name = get_principle_name(principle_id)
                    example = await generate_example_answer(
                        cell_data, assignment_text, p_name, depth
                    )
                    text += f"\n\n💡 *Пример ответа:*\n{example}"
                except Exception as e:
                    logger.warning(f"[Training] Failed to generate example: {e}")

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
            # Показать сохранённое задание заново (без генерации нового)
            saved = await self.load_state(user)
            principle_id = saved.get('principle_id')
            depth = saved.get('depth')
            assignment_text = saved.get('assignment_text', '')
            if principle_id and depth and assignment_text:
                p_name = get_principle_name(principle_id)
                lines = [
                    f"📝 *{p_name}* — Глубина {depth}",
                    f"\n{assignment_text}",
                    "\n✏️ Напишите ваш ответ:",
                ]
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="← Назад", callback_data="train_back")
                ]])
                await self.send(user, '\n'.join(lines), reply_markup=keyboard, parse_mode="Markdown")
            else:
                # Данные потеряны — вернуться к дашборду
                return "back"
            return None
        return None

    async def exit(self, user) -> dict:
        data = await self.load_state(user)
        await self.clear_state(user)
        return data
