"""
Стейт: Первоначальная настройка тренировки.

Вход: из training.dashboard (если setup не пройден) или из настроек
Flow: когнитивный уровень → выбор режима (4 сценария) → дашборд.
"""

from typing import Optional, Dict

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from states.base import BaseState
from engines.training.engine import TrainingEngine
from engines.training.planner import get_principle_name
from config import get_logger, ZP_PRINCIPLES, TRAINING_COGNITIVE_LEVELS

logger = get_logger(__name__)


class TrainingSetupState(BaseState):
    """Настройка тренировки: когнитивный уровень → 4 режима → дашборд."""

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
        # mode_only=True — пропустить когнитивный уровень, только выбор режима
        mode_only = (context or {}).get('mode_only', False)

        self._user_data[chat_id] = {
            'step': 'mode' if mode_only else 'cognitive',
            'cognitive_level': None,
            'training_mode': None,
            'single_principle': None,
        }

        if mode_only:
            await self._show_mode_selection(user)
        else:
            await self._show_cognitive_selection(user)

        return None

    async def _show_cognitive_selection(self, user):
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

    async def _show_mode_selection(self, user):
        """Показать 4 режима тренировки."""
        buttons = [
            [InlineKeyboardButton(
                text="🔀 Все вперемешку",
                callback_data="setup_mode_shuffle"
            )],
            [InlineKeyboardButton(
                text="📶 По порядку из непройденных",
                callback_data="setup_mode_sequential"
            )],
            [InlineKeyboardButton(
                text="🔹 Выбрать нулевой принцип (ZP)",
                callback_data="setup_mode_pick_zp"
            )],
            [InlineKeyboardButton(
                text="🔸 Выбрать первый принцип (FPF)",
                callback_data="setup_mode_pick_fpf"
            )],
        ]

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        text = (
            "📋 *Как тренироваться?*\n\n"
            "🔀 *Все вперемешку* — случайный принцип из непройденных\n"
            "📶 *По порядку* — от первого непройденного к последнему\n"
            "🔹 *Нулевой принцип* — выбрать конкретный ZP\n"
            "🔸 *Первый принцип* — выбрать конкретный FPF (скоро)"
        )
        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    async def _show_pick_zp(self, user):
        """Показать список ZP принципов для выбора."""
        buttons = []
        for pid in ZP_PRINCIPLES:
            name = get_principle_name(pid)
            buttons.append([InlineKeyboardButton(
                text=f"{pid} {name}",
                callback_data=f"setup_pick_{pid}"
            )])
        buttons.append([InlineKeyboardButton(text="← Назад", callback_data="setup_mode_back")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await self.send(user, "🔹 *Выберите нулевой принцип:*",
                        reply_markup=keyboard, parse_mode="Markdown")

    async def handle(self, user, message: Message) -> Optional[str]:
        return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        chat_id = self._get_chat_id(user)
        data = self._user_data.get(chat_id, {})
        cb = callback.data or ''

        step = data.get('step', 'cognitive')

        # === Шаг 1: Когнитивный уровень ===
        if step == 'cognitive' and cb.startswith('setup_cognitive_'):
            level = cb.replace('setup_cognitive_', '')
            if level not in TRAINING_COGNITIVE_LEVELS:
                await callback.answer("Неизвестный уровень", show_alert=True)
                return None
            data['cognitive_level'] = level
            data['step'] = 'mode'
            self._user_data[chat_id] = data
            await callback.answer()
            await self._show_mode_selection(user)
            return None

        # === Шаг 2: Выбор режима ===
        if step == 'mode':
            if cb == 'setup_mode_shuffle':
                data['training_mode'] = 'shuffle'
                self._user_data[chat_id] = data
                await callback.answer("Режим: все вперемешку")
                return await self._finalize(user, chat_id, data)

            if cb == 'setup_mode_sequential':
                data['training_mode'] = 'sequential'
                self._user_data[chat_id] = data
                await callback.answer("Режим: по порядку")
                return await self._finalize(user, chat_id, data)

            if cb == 'setup_mode_pick_zp':
                data['step'] = 'pick_zp'
                self._user_data[chat_id] = data
                await callback.answer()
                await self._show_pick_zp(user)
                return None

            if cb == 'setup_mode_pick_fpf':
                await callback.answer(
                    "Первые принципы (FPF) будут добавлены позже.",
                    show_alert=True
                )
                return None

        # === Шаг 3: Выбор конкретного ZP ===
        if step == 'pick_zp':
            if cb == 'setup_mode_back':
                data['step'] = 'mode'
                self._user_data[chat_id] = data
                await callback.answer()
                await self._show_mode_selection(user)
                return None

            if cb.startswith('setup_pick_'):
                principle_id = cb.replace('setup_pick_', '')
                if principle_id in ZP_PRINCIPLES:
                    data['training_mode'] = 'single'
                    data['single_principle'] = principle_id
                    self._user_data[chat_id] = data
                    name = get_principle_name(principle_id)
                    await callback.answer(f"Выбран: {principle_id} {name}")
                    return await self._finalize(user, chat_id, data)
                await callback.answer("Неизвестный принцип", show_alert=True)
                return None

        return None

    async def _finalize(self, user, chat_id: int, data: dict) -> str:
        """Сохранить настройки и перейти к дашборду."""
        engine = TrainingEngine(chat_id)
        mode = data.get('training_mode', 'shuffle')
        single = data.get('single_principle')

        # Определить enabled_principles на основе режима
        if mode == 'single' and single:
            enabled = [single]
        else:
            enabled = list(ZP_PRINCIPLES)

        cognitive = data.get('cognitive_level')
        if cognitive:
            # Полный setup (первый раз или после смены уровня)
            await engine.setup(
                cognitive_level=cognitive,
                enabled_principles=enabled,
                training_mode=mode,
                single_principle=single,
            )
        else:
            # Только смена режима (mode_only=True)
            await engine.update_training_mode(mode, single)

        return "setup_complete"

    async def exit(self, user) -> dict:
        chat_id = self._get_chat_id(user)
        self._user_data.pop(chat_id, None)
        return {}
