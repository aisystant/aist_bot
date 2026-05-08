"""
Стейт: Главное меню — tier-based ReplyKeyboard (WP-52).

Вход: после онбординга, /mode, /start (existing user), возврат из сервиса
Выход: через ReplyKeyboard → reply_keyboard handler → dispatcher

Меню = tier-based 2x2 ReplyKeyboard (tier_config.py / tier_ui.py).
Сервисы не на клавиатуре доступны через /command или /help.
"""

import asyncio
import random
from typing import Optional

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from core.tier_ui import build_reply_keyboard, sync_menu_commands
from core.tier_detector import detect_ui_tier
from core.tier_config import UITier
from i18n import t, get_language_name, SUPPORTED_LANGUAGES
from db.queries.users import update_intern


# Tier → i18n key suffix for tier label
_TIER_LABEL_KEY = {
    UITier.T0: 't0',
    UITier.T1: 't1',
    UITier.T2_LEARNING: 't2',
    UITier.T3_PERSONALIZATION: 't3',
    UITier.T4_CREATION: 't4',
    UITier.T5_ADMIN: 't4',
}

# Tier → i18n tips key suffix
_TIER_TIPS_KEY = {
    UITier.T0: 't0',
    UITier.T1: 't1',
    UITier.T2_LEARNING: 't2',
    UITier.T3_PERSONALIZATION: 't3',
    UITier.T4_CREATION: 't4',
    UITier.T5_ADMIN: 't4',
}

# Track last tip index per user to avoid consecutive repeats
_last_tip: dict[int, int] = {}


def _get_tier_label(tier: int, lang: str) -> str:
    """Get tier label string for greeting."""
    key = _TIER_LABEL_KEY.get(tier, 't1')
    return t(f'welcome.tier_label.{key}', lang)


def _get_random_tip(tier: int, lang: str, chat_id: int) -> str:
    """Get a random tip for the tier, avoiding consecutive repeats.

    Tips are stored as numbered i18n keys: welcome.tips.t{N}_{idx}
    """
    key_prefix = _TIER_TIPS_KEY.get(tier, 't1')

    # Collect all tips for this tier (t0_0, t0_1, ...)
    tip_texts = []
    for idx in range(10):  # max 10 tips per tier
        text = t(f'welcome.tips.{key_prefix}_{idx}', lang)
        if text.startswith('welcome.tips.'):
            break  # key not found = end of tips
        tip_texts.append(text)

    if not tip_texts:
        return ""

    # Avoid repeating last tip for this user
    last = _last_tip.get(chat_id, -1)
    if len(tip_texts) > 1:
        candidates = [i for i in range(len(tip_texts)) if i != last]
        idx = random.choice(candidates)
    else:
        idx = 0
    _last_tip[chat_id] = idx
    return tip_texts[idx]


class ModeSelectState(BaseState):
    """
    Стейт главного меню.

    Отправляет tier-based ReplyKeyboard (2x2).
    Навигация через handlers/reply_keyboard.py.
    """

    name = "common.mode_select"
    keyboard_type = "reply"  # WP-52: SM knows this is a reply-keyboard state
    display_name = {"ru": "Главное меню", "en": "Main Menu", "es": "Menú principal", "fr": "Menu principal"}
    allow_global = ["consultation", "notes"]

    def _get_lang(self, user) -> str:
        """Получить язык пользователя."""
        if isinstance(user, dict):
            return user.get('language', 'ru')
        return getattr(user, 'language', 'ru') or 'ru'

    def _user_dict(self, user) -> dict:
        """Привести user к dict для реестра."""
        if isinstance(user, dict):
            return user
        return {
            'chat_id': getattr(user, 'chat_id', None),
            'language': getattr(user, 'language', 'ru'),
            'mode': getattr(user, 'mode', 'marathon'),
        }

    async def enter(self, user, context: dict = None) -> None:
        """
        Показываем tier-based ReplyKeyboard.

        Если context содержит day_completed=True — не показываем меню.
        Если context содержит source='mode' — показываем информативное меню с тиром.
        """
        context = context or {}
        lang = self._get_lang(user)

        # После завершения дня не показываем меню
        if context.get('day_completed'):
            return

        user_dict = self._user_dict(user)

        # Tier-based ReplyKeyboard + Menu ☰ sync (WP-52 v4)
        chat_id = user_dict['chat_id']
        tier = await detect_ui_tier(chat_id)
        keyboard = build_reply_keyboard(tier, lang)

        tier_label = _get_tier_label(tier, lang)
        tip = _get_random_tip(tier, lang, chat_id)

        if context.get('source') == 'mode':
            # /mode — информативное сообщение с номером тира
            greeting = t('welcome.mode_menu', lang, tier_num=tier, tier_label=tier_label, tip=tip)
        else:
            # /start и другие входы — приветствие с именем
            name = user_dict.get('name', '')
            greeting = t('welcome.menu_greeting', lang, name=name, tier_label=tier_label, tip=tip)

        await self.send(user, greeting, reply_markup=keyboard, parse_mode="Markdown")
        # fire-and-forget: set_my_commands не блокирует ответ пользователю
        asyncio.create_task(sync_menu_commands(self.bot, chat_id, tier, lang))

        # WP-151 Ф2: показать вопросы о стиле подачи после первого урока
        # user_dict уже загружен выше — не делаем повторный get_intern()
        ctx = user_dict.get('current_context', {}) or {}
        if ctx.get('delivery_prefs_pending'):
            from integrations.telegram.keyboards import kb_delivery_format
            await self.send(user, t('delivery.ask_format', lang),
                            reply_markup=kb_delivery_format(lang))

        # WP-156: предложить Навигатора при возврате после паузы >7 дней
        last_active = user_dict.get('last_active_date')
        if last_active:
            from db.queries.users import moscow_today
            from datetime import date
            days_inactive = (moscow_today() - last_active).days if isinstance(last_active, date) else 0

            if days_inactive >= 7 and not ctx.get('navigator_pause_offered'):
                nav_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🧭 " + t('onboarding.navigator_hint', lang),
                        callback_data="start_navigator",
                    )
                ]])
                await self.send(
                    user,
                    t('welcome.pause_navigator', lang, days=days_inactive),
                    reply_markup=nav_kb,
                )
                ctx['navigator_pause_offered'] = True
                await update_intern(chat_id, current_context=ctx)
            elif days_inactive < 7 and ctx.get('navigator_pause_offered'):
                # Пользователь вернулся — сбросить флаг для следующей паузы
                del ctx['navigator_pause_offered']
                await update_intern(chat_id, current_context=ctx)

    async def handle(self, user, message: Message) -> Optional[str]:
        """Текстовый ввод в главном меню → показываем меню заново."""
        await self.enter(user)
        return None  # Остаёмся в стейте

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Inline-кнопки — backwards compat для старых сообщений в чате."""
        data = callback.data

        if data == "show_language":
            await callback.answer()
            return await self._show_language_options(user, callback)

        if data.startswith("lang_"):
            return await self._save_language(user, callback, data)

        # WP-151: delivery prefs из mode_select (pending после первого урока)
        if data.startswith("delf_"):
            return await self._handle_delivery_format(user, callback, data)
        if data.startswith("detl_"):
            return await self._handle_detail_level(user, callback, data)

        return None

    async def _handle_delivery_format(self, user, callback: CallbackQuery, data: str) -> Optional[str]:
        """Сохранить формат подачи и показать вопрос о детализации."""
        from handlers.delivery_prefs import save_delivery_format
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)
        format_value = data.replace("delf_", "")

        await save_delivery_format(chat_id, format_value)
        await callback.answer()

        from integrations.telegram.keyboards import kb_detail_level
        await callback.message.edit_text(
            t('delivery.ask_detail', lang),
            reply_markup=kb_detail_level(lang),
        )
        return None

    async def _handle_detail_level(self, user, callback: CallbackQuery, data: str) -> Optional[str]:
        """Сохранить детализацию и завершить."""
        from handlers.delivery_prefs import save_detail_level
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)
        detail_value = data.replace("detl_", "")

        await save_detail_level(chat_id, detail_value)
        await callback.answer()

        await callback.message.edit_text(
            f"✅ {t('delivery.saved', lang)}",
        )
        return None

    async def _show_language_options(self, user, callback: CallbackQuery) -> Optional[str]:
        """Show language selector (backwards compat for old inline messages)."""
        lang = self._get_lang(user)
        buttons = [
            [InlineKeyboardButton(text=get_language_name(l), callback_data=f"lang_{l}")]
            for l in SUPPORTED_LANGUAGES
        ]
        buttons.append([InlineKeyboardButton(text=t('buttons.back', lang), callback_data="lang_back")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(t('settings.language.title', lang), reply_markup=keyboard)
        return None

    async def _save_language(self, user, callback: CallbackQuery, data: str) -> Optional[str]:
        """Save selected language and rebuild menu."""
        if data == "lang_back":
            await callback.answer()
            await self.enter(user)
            return None

        chat_id = self._get_chat_id(user)
        new_lang = data.replace("lang_", "")
        if new_lang not in SUPPORTED_LANGUAGES:
            new_lang = 'ru'

        await update_intern(chat_id, language=new_lang)
        # Инвалидация пре-генерированного контента (мог быть на старом языке)
        from db.queries.marathon import invalidate_user_content
        await invalidate_user_content(chat_id)
        if isinstance(user, dict):
            user['language'] = new_lang

        await callback.answer(t('settings.language.changed', new_lang))

        await self.enter(user)

        # Подсказка: /start обновляет меню на новом языке (после меню)
        await self.send(user, t('settings.language.restart_hint', new_lang))
        return None

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)
