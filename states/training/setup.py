"""
Стейт: Настройка режима тренировки.

Вход: из training.dashboard (первый запуск или «Сменить режим»)
Flow: выбор режима (4 сценария) -> дашборд.
Когнитивный уровень = postformal (фиксирован, Phase 2 — тренировка ребёнка).
"""

from typing import Optional

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from states.base import BaseState
from engines.training.engine import TrainingEngine
from engines.training.planner import get_principle_name
from config import get_logger, ZP_PRINCIPLES

logger = get_logger(__name__)

# Фиксированный когнитивный уровень (Phase 1 — только взрослые)
DEFAULT_COGNITIVE_LEVEL = 'postformal'


class TrainingSetupState(BaseState):
    """Настройка тренировки: выбор режима -> дашборд."""

    name = "training.setup"
    display_name = {
        "ru": "Настройка тренировки",
        "en": "Training Setup",
    }
    allow_global = []

    async def enter(self, user, context: dict = None) -> Optional[str]:
        await self.save_state(user, {
            'step': 'mode',
            'training_mode': None,
            'single_principle': None,
        })

        await self._show_mode_selection(user)
        return None

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

        buttons.append([InlineKeyboardButton(text="← Назад", callback_data="setup_back")])

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
        data = await self.load_state(user)
        cb = callback.data or ''

        step = data.get('step', 'mode')

        # === Назад ===
        if cb == 'setup_back':
            await callback.answer()
            return "back"

        # === Выбор режима ===
        if step == 'mode':
            if cb == 'setup_mode_shuffle':
                data = {**data, 'training_mode': 'shuffle'}
                await self.save_state(user, data)
                await callback.answer("Режим: все вперемешку")
                return await self._finalize(user, data)

            if cb == 'setup_mode_sequential':
                data = {**data, 'training_mode': 'sequential'}
                await self.save_state(user, data)
                await callback.answer("Режим: по порядку")
                return await self._finalize(user, data)

            if cb == 'setup_mode_pick_zp':
                await self.save_state(user, {**data, 'step': 'pick_zp'})
                await callback.answer()
                await self._show_pick_zp(user)
                return None

            if cb == 'setup_mode_pick_fpf':
                await callback.answer(
                    "Первые принципы (FPF) будут добавлены позже.",
                    show_alert=True
                )
                return None

        # === Выбор конкретного ZP ===
        if step == 'pick_zp':
            if cb == 'setup_mode_back':
                await self.save_state(user, {**data, 'step': 'mode'})
                await callback.answer()
                await self._show_mode_selection(user)
                return None

            if cb.startswith('setup_pick_'):
                principle_id = cb.replace('setup_pick_', '')
                if principle_id in ZP_PRINCIPLES:
                    data = {**data, 'training_mode': 'single', 'single_principle': principle_id}
                    await self.save_state(user, data)
                    name = get_principle_name(principle_id)
                    await callback.answer(f"Выбран: {principle_id} {name}")
                    return await self._finalize(user, data)
                await callback.answer("Неизвестный принцип", show_alert=True)
                return None

        return None

    async def _finalize(self, user, data: dict) -> str:
        """Сохранить настройки и перейти к дашборду."""
        chat_id = self._get_chat_id_from_user(user)
        engine = TrainingEngine(chat_id)
        mode = data.get('training_mode', 'shuffle')
        single = data.get('single_principle')

        # Определить enabled_principles на основе режима
        if mode == 'single' and single:
            enabled = [single]
        else:
            enabled = list(ZP_PRINCIPLES)

        # Проверяем: первый setup или смена режима
        is_first_setup = not await engine.is_setup_complete()
        if is_first_setup:
            # Первый раз — полный setup с cognitive_level=postformal
            await engine.setup(
                cognitive_level=DEFAULT_COGNITIVE_LEVEL,
                enabled_principles=enabled,
                training_mode=mode,
                single_principle=single,
            )
        else:
            # Смена режима
            await engine.update_training_mode(mode, single)

        return "setup_complete"

    async def exit(self, user) -> dict:
        await self.clear_state(user)
        return {}
