"""
Стейт: Бонусный вопрос Марафона.

Вход: после правильного ответа на вопрос урока (если bloom_level >= 2)
Выход: workshop.marathon.task (задание)

Логика:
- Уровень 1: бонус НЕ предлагается (сразу задание)
- Уровни 2 и 3: бонус предлагается (можно отказаться)
"""

from typing import Optional

from aiogram.types import Message

from states.base import BaseState
from i18n import t
from db.queries import update_intern, save_answer


class MarathonBonusState(BaseState):
    """
    Стейт бонусного вопроса Марафона.

    Предлагает ученику вопрос повышенной сложности.
    Необязательный — можно отказаться и сразу перейти к заданию.
    """

    name = "workshop.marathon.bonus"
    display_name = {"ru": "Бонусный вопрос", "en": "Bonus Question"}
    allow_global = ["consultation", "notes"]

    # Тексты кнопок
    YES_BUTTONS = ["🚀 Да, давай сложнее!", "🚀 Yes, harder!", "🚀 Sí, más difícil"]
    NO_BUTTONS = ["✅ Достаточно", "✅ Enough", "✅ Suficiente"]

    def _get_lang(self, user) -> str:
        """Получить язык пользователя."""
        if isinstance(user, dict):
            return user.get('language', 'ru')
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        """Получить chat_id пользователя."""
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    async def enter(self, user, context: dict = None) -> None:
        """
        Предлагаем бонусный вопрос.

        Context может содержать:
        - topic_index: индекс текущей темы
        - topic_name: название темы
        - bloom_level: текущий уровень сложности
        """
        lang = self._get_lang(user)
        context = context or {}

        # Показываем кнопки выбора бонусного вопроса
        yes_btn = t('buttons.bonus_yes', lang)
        no_btn = t('buttons.bonus_no', lang)

        buttons = [[yes_btn], [no_btn]]
        await self.send_with_keyboard(
            user,
            t('marathon.want_harder', lang),
            buttons
        )

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем выбор/ответ пользователя.

        Возможные сценарии:
        1. Пользователь нажал "Да" — генерируем вопрос и ждём ответ
        2. Пользователь нажал "Нет" — переходим к заданию
        3. Пользователь отвечает на вопрос — проверяем и переходим к заданию
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # Пользователь хочет бонусный вопрос
        if self._is_yes_button(text, lang):
            # Генерируем бонусный вопрос через LLM
            # В текущей реализации это делается в callback handler в bot.py
            # Здесь просто показываем сообщение о генерации
            await self.send(user, t('marathon.generating_harder', lang))
            return "yes"

        # Пользователь отказался
        if self._is_no_button(text, lang):
            await self.send(user, t('marathon.loading_practice', lang))
            return "no"

        # Это ответ на бонусный вопрос (текст минимум 20 символов)
        if len(text) >= 20:
            # Сохраняем ответ
            if chat_id:
                await save_answer(
                    chat_id=chat_id,
                    topic_index=0,  # TODO: получить из контекста
                    answer=f"[BONUS] {text}",
                    answer_type="bonus_answer"
                )

            await self.send(user, t('marathon.bonus_completed', lang))
            return "answered"

        # Слишком короткий ответ — показываем ожидание
        await self.send(user, f"{t('marathon.waiting_for', lang)}: {t('marathon.answer_expected', lang)}")
        return None  # Остаёмся в стейте

    def _is_yes_button(self, text: str, lang: str) -> bool:
        """Проверяем, нажал ли пользователь кнопку 'Да'."""
        text_lower = text.lower()
        yes_btn = t('buttons.bonus_yes', lang).lower()

        if text_lower == yes_btn:
            return True
        if text_lower in [b.lower() for b in self.YES_BUTTONS]:
            return True
        if "да" in text_lower or "yes" in text_lower or "harder" in text_lower:
            return True

        return False

    def _is_no_button(self, text: str, lang: str) -> bool:
        """Проверяем, нажал ли пользователь кнопку 'Нет'."""
        text_lower = text.lower()
        no_btn = t('buttons.bonus_no', lang).lower()

        if text_lower == no_btn:
            return True
        if text_lower in [b.lower() for b in self.NO_BUTTONS]:
            return True
        if "достаточно" in text_lower or "enough" in text_lower:
            return True

        return False

    async def exit(self, user) -> dict:
        """Передаём контекст следующему стейту (заданию)."""
        return {"from_bonus": True}
