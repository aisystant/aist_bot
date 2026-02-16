"""
Базовый класс для всех стейтов State Machine.

Каждый стейт — это отдельный файл в соответствующей папке:
- states/common/ — общие стейты (start, mode_select, error, consultation)
- states/workshops/ — стейты мастерских (marathon, exocortex, ...)
- states/feed/ — стейты Ленты (topics, digest)
- states/utilities/ — стейты утилит (notes, export)

Пример создания нового стейта:

    from states.base import BaseState

    class MyState(BaseState):
        name = "category.my_state"
        display_name = {"ru": "Мой стейт", "en": "My State", "es": "Mi estado", "fr": "Mon état"}

        async def enter(self, user, context=None):
            await self.send(user, self.t("my_state.welcome", user))

        async def handle(self, user, message):
            # Обработка сообщения
            return "next_event"  # или None чтобы остаться
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

from aiogram import Bot
from aiogram.types import Message, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

logger = logging.getLogger(__name__)


class BaseState(ABC):
    """
    Базовый класс для всех стейтов.

    Один стейт = один файл.

    Атрибуты класса:
        name: Уникальный идентификатор стейта (формат: "category.subcategory.name")
        display_name: Человекочитаемое название для логов {lang: название}
        allow_global: Список глобальных команд, доступных в этом стейте
    """

    # Уникальный идентификатор стейта
    # Формат: "category.name" или "category.subcategory.name"
    # Примеры: "common.start", "workshop.marathon.lesson", "common.consultation"
    name: str = "base"

    # Человекочитаемое название для логов и отладки
    display_name: dict[str, str] = {"ru": "Базовый стейт", "en": "Base State"}

    # Глобальные команды, доступные в этом стейте
    # Эти команды вызывают переход независимо от логики стейта
    # Примеры: ["consultation", "notes"]
    allow_global: list[str] = []

    # Тип клавиатуры: "inline" (default), "reply", "none"
    # SM engine автоматически отправляет ReplyKeyboardRemove при переходе reply → non-reply.
    # Полная карта: CLAUDE.md § 10.5
    keyboard_type: str = "inline"

    # Pending keyboard cleanup (class-level, shared across all states).
    # SM engine записывает сюда ReplyKeyboardRemove при reply→non-reply переходе.
    # Первый send() нового стейта прикрепляет cleanup автоматически.
    _pending_keyboard_cleanup: dict = {}

    def __init__(self, bot: Bot, db, llm, i18n):
        """
        Args:
            bot: Telegram bot instance
            db: Database repository (для работы с БД)
            llm: LLM client (Claude)
            i18n: Localization service
        """
        self.bot = bot
        self.db = db
        self.llm = llm
        self.i18n = i18n

    async def enter(self, user, context: dict = None) -> None:
        """
        Вызывается при ВХОДЕ в стейт.

        Здесь обычно отправляется приветственное сообщение
        и выполняется начальная настройка.

        Args:
            user: Объект пользователя из БД
            context: Дополнительные данные от предыдущего стейта
        """
        pass

    @abstractmethod
    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатывает входящее сообщение.

        Это главный метод стейта — здесь реализуется логика обработки.

        Args:
            user: Объект пользователя
            message: Сообщение от Telegram

        Returns:
            Событие для перехода (str) или None если остаёмся в стейте.
            Примеры событий: "correct", "skip", "done", "error"
        """
        pass

    async def exit(self, user) -> dict:
        """
        Вызывается при ВЫХОДЕ из стейта.

        Здесь можно очистить временные данные и подготовить
        контекст для следующего стейта.

        Args:
            user: Объект пользователя

        Returns:
            Контекст для передачи следующему стейту
        """
        return {}

    # =========================================
    # Вспомогательные методы
    # =========================================

    def t(self, key: str, user, **kwargs) -> str:
        """
        Shortcut для локализации.

        Args:
            key: Ключ перевода (например, "marathon.welcome")
            user: Объект пользователя (для определения языка)
            **kwargs: Параметры для форматирования

        Returns:
            Переведённая строка
        """
        lang = getattr(user, 'language', 'ru') or 'ru'
        return self.i18n.t(key, lang, **kwargs)

    async def send(self, user, text: str, **kwargs) -> Message:
        """
        Shortcut для отправки сообщения.

        Keyboard cleanup (reply→non-reply переход):
        - Без reply_markup: прикрепляет ReplyKeyboardRemove к сообщению.
        - С InlineKeyboardMarkup: отправляет текст с ReplyKeyboardRemove,
          затем edit_reply_markup для InlineKeyboard (Telegram API не позволяет
          совместить ReplyKeyboardRemove и InlineKeyboard в одном сообщении).
        - С другим reply_markup (ReplyKeyboard): пропускает cleanup (новая
          Reply-клавиатура заменяет старую).

        Args:
            user: Объект пользователя
            text: Текст сообщения
            **kwargs: Дополнительные параметры для send_message

        Returns:
            Отправленное сообщение
        """
        telegram_id = (
            getattr(user, 'telegram_id', None) or
            getattr(user, 'chat_id', None) or
            (user.get('telegram_id') or user.get('chat_id') if isinstance(user, dict) else None)
        )
        # Auto-cleanup: SM engine sets pending cleanup on reply→non-reply transition
        if telegram_id:
            pending = BaseState._pending_keyboard_cleanup.pop(telegram_id, None)
            if pending:
                reply_markup = kwargs.get('reply_markup')
                if reply_markup is None:
                    # Simple case: no markup → attach cleanup directly
                    kwargs['reply_markup'] = pending
                elif isinstance(reply_markup, InlineKeyboardMarkup):
                    # InlineKeyboard doesn't remove ReplyKeyboard — they're independent
                    # in Telegram API. Send with ReplyKeyboardRemove first, then
                    # edit to attach InlineKeyboard (1 extra API call, only on transition).
                    inline_kb = kwargs.pop('reply_markup')
                    kwargs['reply_markup'] = pending
                    msg = await self.bot.send_message(telegram_id, text, **kwargs)
                    try:
                        await self.bot.edit_message_reply_markup(
                            chat_id=telegram_id,
                            message_id=msg.message_id,
                            reply_markup=inline_kb,
                        )
                        logger.info(f"[Keyboard] send+edit OK for chat {telegram_id}")
                    except Exception as e:
                        logger.warning(f"[Keyboard] edit_reply_markup failed for chat {telegram_id}: {e}, trying edit_text")
                        # Fallback: edit_text replaces text+reply_markup at once.
                        # Do NOT delete the message — ReplyKeyboardRemove must persist.
                        try:
                            msg = await msg.edit_text(
                                text,
                                reply_markup=inline_kb,
                                parse_mode=kwargs.get('parse_mode'),
                            )
                        except Exception as e2:
                            logger.warning(f"[Keyboard] edit_text also failed for chat {telegram_id}: {e2}")
                            # Message stays without inline buttons but reply keyboard is removed
                    return msg
                # else: ReplyKeyboardMarkup — replaces old keyboard, cleanup not needed
        return await self.bot.send_message(telegram_id, text, **kwargs)

    async def send_with_keyboard(
        self,
        user,
        text: str,
        buttons: list[list[str]],
        one_time: bool = True,
        resize: bool = True
    ) -> Message:
        """
        Отправка с reply keyboard.

        Args:
            user: Объект пользователя
            text: Текст сообщения
            buttons: Двумерный список с текстом кнопок [[row1_btn1, row1_btn2], [row2_btn1]]
            one_time: Скрыть клавиатуру после нажатия
            resize: Подогнать размер кнопок

        Returns:
            Отправленное сообщение
        """
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=btn) for btn in row] for row in buttons],
            resize_keyboard=resize,
            one_time_keyboard=one_time
        )
        return await self.send(user, text, reply_markup=keyboard)

    async def send_remove_keyboard(self, user, text: str) -> Message:
        """
        Отправка с удалением клавиатуры.

        Args:
            user: Объект пользователя
            text: Текст сообщения

        Returns:
            Отправленное сообщение
        """
        return await self.send(user, text, reply_markup=ReplyKeyboardRemove())

    def get_display_name(self, lang: str = "ru") -> str:
        """
        Получить человекочитаемое название стейта.

        Args:
            lang: Код языка

        Returns:
            Название стейта на указанном языке
        """
        return self.display_name.get(lang, self.display_name.get("ru", self.name))

    def __repr__(self) -> str:
        return f"<State: {self.name}>"
