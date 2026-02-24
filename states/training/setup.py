"""
Стейт: Первоначальная настройка тренировки.

Вход: из training.dashboard (если setup не пройден) или из настроек
Выбор когнитивного уровня + принципов.
"""

from typing import Optional, Dict

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from states.base import BaseState
from engines.training.engine import TrainingEngine
from config import get_logger, ZP_PRINCIPLES, TRAINING_COGNITIVE_LEVELS

logger = get_logger(__name__)


class TrainingSetupState(BaseState):
    """Настройка тренировки: когнитивный уровень + выбор принципов."""

    name = "training.setup"
    display_name = {
        "ru": "Настройка тренировки",
        "en": "Training Setup",
    }
    allow_global = []

    _user_data: Dict[int, Dict] = {}

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    async def enter(self, user, context: dict = None) -> Optional[str]:
        chat_id = self._get_chat_id(user)
        self._user_data[chat_id] = {
            'step': 'cognitive',
            'cognitive_level': None,
            'selected_principles': list(ZP_PRINCIPLES),
        }

        buttons = []
        for key, label in TRAINING_COGNITIVE_LEVELS.items():
            buttons.append([InlineKeyboardButton(
                text=label, callback_data=f"setup_cognitive_{key}"
            )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await self.send(
            user,
            "🧠 *Тренировка принципов мышления*\n\n"
            "Выберите когнитивный уровень тренируемого:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        return None

    async def handle(self, user, message: Message) -> Optional[str]:
        return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        chat_id = self._get_chat_id(user)
        data = self._user_data.get(chat_id, {})
        cb = callback.data or ''

        step = data.get('step', 'cognitive')

        if step == 'cognitive' and cb.startswith('setup_cognitive_'):
            level = cb.replace('setup_cognitive_', '')
            if level not in TRAINING_COGNITIVE_LEVELS:
                await callback.answer("Неизвестный уровень", show_alert=True)
                return None
            data['cognitive_level'] = level
            data['step'] = 'principles'
            self._user_data[chat_id] = data
            await callback.answer()
            await self._show_principles(user, data)
            return None

        if step == 'principles':
            if cb.startswith('setup_toggle_'):
                target = cb.replace('setup_toggle_', '')
                selected = data.get('selected_principles', [])
                if target == 'all':
                    if len(selected) == len(ZP_PRINCIPLES):
                        selected = [ZP_PRINCIPLES[0]]
                    else:
                        selected = list(ZP_PRINCIPLES)
                elif target in ZP_PRINCIPLES:
                    if target in selected:
                        if len(selected) > 1:
                            selected.remove(target)
                    else:
                        selected.append(target)
                data['selected_principles'] = selected
                self._user_data[chat_id] = data
                await callback.answer()
                await self._show_principles(user, data)
                return None

            if cb == 'setup_confirm':
                engine = TrainingEngine(chat_id)
                await engine.setup(
                    data.get('cognitive_level', 'postformal'),
                    data.get('selected_principles', list(ZP_PRINCIPLES))
                )
                await callback.answer("Настройки сохранены!")
                return "setup_complete"

        return None

    async def _show_principles(self, user, data: dict):
        """Показать выбор принципов."""
        from engines.training.planner import load_zp_cells
        cells = load_zp_cells()
        selected = data.get('selected_principles', [])

        buttons = []
        for pid in ZP_PRINCIPLES:
            name = cells.get(pid, {}).get('name', pid)
            check = '✅' if pid in selected else '⬜'
            buttons.append([InlineKeyboardButton(
                text=f"{check} {pid} {name}",
                callback_data=f"setup_toggle_{pid}"
            )])

        all_selected = len(selected) == len(ZP_PRINCIPLES)
        buttons.append([InlineKeyboardButton(
            text="☑️ Все" if all_selected else "⬜ Все",
            callback_data="setup_toggle_all"
        )])
        buttons.append([InlineKeyboardButton(
            text="✅ Подтвердить", callback_data="setup_confirm"
        )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await self.send(user, "Выберите принципы для тренировки:", reply_markup=keyboard)

    async def exit(self, user) -> dict:
        chat_id = self._get_chat_id(user)
        self._user_data.pop(chat_id, None)
        return {}
