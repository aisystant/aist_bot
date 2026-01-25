"""
Стейт: Урок Марафона.

Вход: из common.mode_select (выбор "Марафон") или после завершения задания
Выход: workshop.marathon.question (после показа урока)
"""

from typing import Optional

from aiogram.types import Message

from states.base import BaseState
from locales import t
from db.queries import get_intern, update_intern


class MarathonLessonState(BaseState):
    """
    Стейт показа урока Марафона.

    Показывает теоретический материал и переходит к вопросу.
    """

    name = "workshop.marathon.lesson"
    display_name = {"ru": "Урок Марафона", "en": "Marathon Lesson"}
    allow_global = ["consultant", "notes"]

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

    def _get_marathon_day(self, user) -> int:
        """Получить текущий день марафона."""
        if isinstance(user, dict):
            completed = user.get('completed_topics', [])
        else:
            completed = getattr(user, 'completed_topics', [])
        return len(completed) // 2 + 1

    def _get_current_topic_index(self, user) -> int:
        """Получить индекс текущей темы."""
        if isinstance(user, dict):
            return user.get('current_topic_index', 0)
        return getattr(user, 'current_topic_index', 0)

    def _get_completed_topics(self, user) -> list:
        """Получить список завершённых тем."""
        if isinstance(user, dict):
            return user.get('completed_topics', [])
        return getattr(user, 'completed_topics', [])

    async def enter(self, user, context: dict = None) -> None:
        """
        Показываем урок текущего дня.

        Проверяем:
        - Марафон завершён?
        - Есть доступные темы?
        - Лимит тем на сегодня не превышен?
        """
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        completed = self._get_completed_topics(user)
        marathon_day = self._get_marathon_day(user)
        topic_index = self._get_current_topic_index(user)

        # Проверка: марафон завершён
        if len(completed) >= 28:
            await self.send(user, t('marathon.completed', lang))
            return  # Событие marathon_complete обработает StateMachine

        # Проверка: дневной лимит
        if isinstance(user, dict):
            topics_today = user.get('topics_today', 0)
        else:
            topics_today = getattr(user, 'topics_today', 0)

        if topics_today >= 4:
            await self.send(user, t('marathon.daily_limit', lang))
            return

        # Показываем сообщение о загрузке
        await self.send(user, f"⏳ {t('marathon.generating_material', lang)}")

        # Генерация контента делегируется LLM клиенту
        # В текущей реализации используем заглушку
        # TODO: Интеграция с claude.generate_content()

        await self.send(
            user,
            f"📚 *{t('marathon.day_theory', lang, day=marathon_day)}*\n\n"
            f"_Материал урока будет сгенерирован..._\n\n"
            f"Тема #{topic_index + 1}",
            parse_mode="Markdown"
        )

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем ввод пользователя.

        В этом стейте пользователь обычно просто читает материал.
        Любое сообщение переводит к вопросу.
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)

        # Вопрос к ИИ
        if text.startswith('?'):
            # Обрабатываем вопрос, но остаёмся в стейте
            await self.send(user, t('marathon.question_processed', lang))
            return None

        # Готов к вопросу
        return "lesson_shown"

    async def exit(self, user) -> dict:
        """Передаём контекст следующему стейту."""
        return {
            "topic_index": self._get_current_topic_index(user),
            "marathon_day": self._get_marathon_day(user)
        }
