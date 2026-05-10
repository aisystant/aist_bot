from __future__ import annotations

"""
Стейт: Занятие с ребёнком (Phase 2).

Вход: из training.dashboard (пользователь выбрал ребёнка)
Показывает дашборд ребёнка -> генерирует карточку занятия ->
взрослый отмечает результат (self-report или ввод ответа ребёнка).
"""

from typing import Optional

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from states.base import BaseState
from engines.training.engine import TrainingEngine
from engines.training.planner import get_kid_principle_name
from config import get_logger, KID_MAX_DEPTH

logger = get_logger(__name__)


def _progress_bar(current: int, total: int) -> str:
    filled = '\u2588' * current
    empty = '\u2591' * (total - current)
    return f"[{filled}{empty}] {current}/{total}"


class TrainingChildAssignmentState(BaseState):
    """Занятие с ребёнком: дашборд -> задание -> результат."""

    name = "training.child_assignment"
    display_name = {
        "ru": "Занятие с ребёнком",
        "en": "Child Training",
    }
    allow_global = ["consultation", "notes"]

    async def enter(self, user, context: dict = None) -> Optional[str]:
        chat_id = self._get_chat_id_from_user(user)
        child_id = (context or {}).get('child_id')

        if not child_id:
            return "back"

        await self.save_state(user, {'child_id': child_id, 'step': 'dashboard'})
        await self._show_child_dashboard(user, chat_id, child_id)
        return None

    async def _show_child_dashboard(self, user, chat_id: int, child_id: int):  # noqa: ARG002
        """Показать дашборд ребёнка."""
        engine = TrainingEngine(chat_id)
        data = await engine.get_child_dashboard_data(child_id)
        if not data:
            await self.send(user, "Ошибка загрузки данных ребёнка.")
            return

        child = data['child']
        principles = data['principles']
        stats = data['stats']

        lines = [
            f"👶 *Занятие с {child['name']}*",
            "━━━━━━━━━━━━━━━━━━",
        ]

        has_incomplete = False
        for p in principles:
            bar = _progress_bar(p['current_depth'], p['max_depth'])
            status = "✅" if p['completed'] else ""
            lines.append(f"{p['id']} {p['name']}  {bar} {status}")
            if not p['completed']:
                has_incomplete = True

        lines.append("━━━━━━━━━━━━━━━━━━")
        total = stats.get('total_attempts', 0)
        passed = stats.get('total_passed', 0)
        if total > 0:
            lines.append(f"Занятий: {total} | Пройдено: {passed}")

        buttons = []
        if has_incomplete:
            buttons.append([InlineKeyboardButton(
                text="▶️ Начать занятие",
                callback_data="child_continue"
            )])

        # Выбор конкретного принципа
        for p in principles:
            if not p['completed']:
                buttons.append([InlineKeyboardButton(
                    text=f"{p['id']} {p['name']}",
                    callback_data=f"child_principle_{p['id']}"
                )])

        buttons.append([InlineKeyboardButton(
            text="← Назад",
            callback_data="child_back"
        )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await self.send(user, '\n'.join(lines), reply_markup=keyboard, parse_mode="Markdown")

    async def _show_assignment(self, user, chat_id: int, child_id: int, principle_id: str):
        """Показать карточку занятия."""
        engine = TrainingEngine(chat_id)
        assignment = await engine.generate_child_assignment(child_id, principle_id)

        if not assignment:
            p_name = get_kid_principle_name(principle_id)
            await self.send(user, f"✅ {p_name} — пройден полностью.")
            await self._show_child_dashboard(user, chat_id, child_id)
            return

        await self.save_state(user, {
            'child_id': child_id,
            'step': 'assignment',
            'principle_id': principle_id,
            'depth': assignment['depth'],
            'assignment_text': assignment['assignment_text'],
        })

        lines = [
            f"👶 *Занятие для {assignment['child_name']}*",
            f"Принцип: {assignment['principle_name']} — Глубина {assignment['depth']}",
            "━━━━━━━━━━━━━━━━━━",
            "",
            assignment['assignment_text'],
            "",
            "━━━━━━━━━━━━━━━━━━",
            "Проведите занятие и отметьте результат:",
        ]

        buttons = [
            [InlineKeyboardButton(
                text="✅ Выполнено",
                callback_data="child_done"
            )],
            [InlineKeyboardButton(
                text="✏️ Ввести ответ ребёнка",
                callback_data="child_enter_answer"
            )],
            [InlineKeyboardButton(
                text="❌ Не получилось",
                callback_data="child_failed"
            )],
            [InlineKeyboardButton(
                text="← Назад",
                callback_data="child_assignment_back"
            )],
        ]

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await self.send(user, '\n'.join(lines), reply_markup=keyboard, parse_mode="Markdown")

    async def handle(self, user, message: Message) -> Optional[str]:
        chat_id = self._get_chat_id_from_user(user)
        data = await self.load_state(user)
        text = (message.text or '').strip()

        # Ожидание ответа ребёнка
        if data.get('step') == 'waiting_answer' and text:
            child_id = data.get('child_id')
            principle_id = data.get('principle_id')
            depth = data.get('depth')
            assignment_text = data.get('assignment_text', '')

            if not child_id or not principle_id or not depth:
                return "back"

            await self.send(user, "🔍 Оцениваю ответ...")

            engine = TrainingEngine(chat_id)
            result = await engine.report_child_result(
                child_id, principle_id, depth,
                assignment_text, passed=False, answer_text=text,
            )

            if result.get('passed'):
                new_depth = result.get('new_depth', depth)
                msg = f"✅ *Принято!* Глубина {new_depth}/{KID_MAX_DEPTH}"
                if result.get('feedback'):
                    msg += f"\n\n{result['feedback']}"
            else:
                msg = f"🔶 *Пока не совсем*"
                if result.get('feedback'):
                    msg += f"\n\n{result['feedback']}"

            buttons = [
                [InlineKeyboardButton(text="← К дашборду", callback_data="child_to_dashboard")],
            ]
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await self.send(user, msg, reply_markup=keyboard, parse_mode="Markdown")

            await self.save_state(user, {**data, 'step': 'result'})
            return None

        return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        cb = callback.data or ''
        chat_id = self._get_chat_id_from_user(user)
        data = await self.load_state(user)
        child_id = data.get('child_id')

        if cb == 'child_back':
            await callback.answer()
            return "back"

        if cb == 'child_continue':
            if not child_id:
                return "back"
            engine = TrainingEngine(chat_id)
            next_pid = await engine.get_next_child_principle(child_id)
            if next_pid:
                await callback.answer("Генерирую занятие...")
                await self._show_assignment(user, chat_id, child_id, next_pid)
            else:
                await callback.answer("Все принципы пройдены!", show_alert=True)
            return None

        if cb.startswith('child_principle_'):
            principle_id = cb.replace('child_principle_', '')
            if not child_id:
                return "back"
            await callback.answer("Генерирую занятие...")
            await self._show_assignment(user, chat_id, child_id, principle_id)
            return None

        if cb == 'child_done':
            # Self-report: выполнено
            principle_id = data.get('principle_id')
            depth = data.get('depth')
            assignment_text = data.get('assignment_text', '')
            if not child_id or not principle_id or not depth:
                return "back"

            engine = TrainingEngine(chat_id)
            result = await engine.report_child_result(
                child_id, principle_id, depth,
                assignment_text, passed=True,
            )

            new_depth = result.get('new_depth', depth)
            if new_depth and new_depth >= KID_MAX_DEPTH:
                msg = f"🎉 *Принцип пройден полностью!* ({new_depth}/{KID_MAX_DEPTH})"
            else:
                msg = f"✅ *Отлично!* Глубина {new_depth}/{KID_MAX_DEPTH}"

            buttons = [
                [InlineKeyboardButton(text="← К дашборду", callback_data="child_to_dashboard")],
            ]
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await callback.answer("Записано!")
            await self.send(user, msg, reply_markup=keyboard, parse_mode="Markdown")

            await self.save_state(user, {**data, 'step': 'result'})
            return None

        if cb == 'child_enter_answer':
            await self.save_state(user, {**data, 'step': 'waiting_answer'})
            await callback.answer()
            await self.send(user, "✏️ Введите ответ ребёнка (что он сказал/сделал):")
            return None

        if cb == 'child_failed':
            # Self-report: не получилось
            principle_id = data.get('principle_id')
            depth = data.get('depth')
            assignment_text = data.get('assignment_text', '')
            if not child_id or not principle_id or not depth:
                return "back"

            engine = TrainingEngine(chat_id)
            await engine.report_child_result(
                child_id, principle_id, depth,
                assignment_text, passed=False,
            )

            buttons = [
                [InlineKeyboardButton(text="🔄 Попробовать ещё", callback_data=f"child_principle_{principle_id}")],
                [InlineKeyboardButton(text="← К дашборду", callback_data="child_to_dashboard")],
            ]
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await callback.answer()
            await self.send(
                user,
                "📝 Ничего страшного! Можно попробовать позже или перейти к другому принципу.",
                reply_markup=keyboard,
            )

            await self.save_state(user, {**data, 'step': 'result'})
            return None

        if cb == 'child_assignment_back':
            await callback.answer()
            if child_id:
                await self.save_state(user, {**data, 'step': 'dashboard'})
                await self._show_child_dashboard(user, chat_id, child_id)
            return None

        if cb == 'child_to_dashboard':
            await callback.answer()
            if child_id:
                await self.save_state(user, {**data, 'step': 'dashboard'})
                await self._show_child_dashboard(user, chat_id, child_id)
            return None

        return None

    async def exit(self, user) -> dict:
        data = await self.load_state(user)
        await self.clear_state(user)
        return data
