"""
Стейт: Выбор тем для Ленты.

Вход: из common.mode_select (выбор "Лента") или после завершения недели
Выход: feed.digest (после выбора тем)
"""

from typing import Optional

from aiogram.types import Message

from states.base import BaseState
from i18n import t


class FeedTopicsState(BaseState):
    """
    Стейт выбора тем для Ленты.

    Генерирует 5 тем, показывает меню, принимает выбор (1-3 темы).
    """

    name = "feed.topics"
    display_name = {"ru": "Выбор тем", "en": "Topic Selection"}
    allow_global = ["consultation", "notes"]

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
        Показываем меню выбора тем.

        Context может содержать:
        - new_week: True если это начало новой недели
        - suggested_topics: уже сгенерированные темы
        """
        lang = self._get_lang(user)
        context = context or {}

        # Показываем индикатор генерации
        await self.send(user, f"⏳ {t('feed.generating_topics', lang)}")

        # Генерация тем делегируется LLM клиенту
        # TODO: Интеграция с engines/feed/planner.py

        # Заглушка: 5 тем
        topics = context.get('suggested_topics', [
            "Собранность",
            "Внимание",
            "Рефлексия",
            "Целеполагание",
            "Привычки",
        ])

        # Формируем сообщение
        topics_text = "\n".join([
            f"{i+1}️⃣ *{topic}*"
            for i, topic in enumerate(topics)
        ])

        await self.send(
            user,
            f"📋 *{t('feed.choose_topics', lang)}*\n\n"
            f"{topics_text}\n\n"
            f"_{t('feed.choose_hint', lang)}_\n\n"
            f"💬 *{t('feed.waiting_for', lang)}:* {t('feed.topic_selection', lang)}",
            parse_mode="Markdown"
        )

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем выбор тем.

        Returns:
        - "topics_selected" → digest
        - "skip" → mode_select
        - None → остаёмся (неверный формат)
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # Вопрос к ИИ
        if text.startswith('?'):
            question = text[1:].strip()
            if question:
                await self.send(
                    user,
                    f"_Ответ на ваш вопрос..._\n\n"
                    f"💬 *{t('feed.waiting_for', lang)}:* {t('feed.topic_selection', lang)}",
                    parse_mode="Markdown"
                )
            return None

        # Пропуск
        if "пропустить" in text.lower() or "skip" in text.lower():
            await self.send(user, t('feed.topic_selection_skipped', lang))
            return "skip"

        # Парсинг выбора тем (1, 2, 3 или "тема 1 и тема 2")
        selected = self._parse_topic_selection(text)

        if not selected or len(selected) > 3:
            await self.send(
                user,
                f"{t('feed.invalid_selection', lang)}\n\n"
                f"💬 *{t('feed.waiting_for', lang)}:* {t('feed.topic_selection', lang)}",
                parse_mode="Markdown"
            )
            return None

        # TODO: Сохранить выбранные темы в feed_weeks
        # await save_feed_week(chat_id, accepted_topics=selected)

        # Подтверждение
        topics_str = ", ".join(selected)
        await self.send(
            user,
            f"✅ *{t('feed.topics_selected', lang)}*\n{topics_str}",
            parse_mode="Markdown"
        )

        return "topics_selected"

    def _parse_topic_selection(self, text: str) -> list[str]:
        """
        Парсит выбор тем из текста.

        Поддерживает:
        - "1, 2, 3" → индексы
        - "1 и 3" → индексы
        - "собранность, внимание" → названия
        """
        # Заглушка: темы по умолчанию
        default_topics = [
            "Собранность",
            "Внимание",
            "Рефлексия",
            "Целеполагание",
            "Привычки",
        ]

        selected = []

        # Ищем цифры
        import re
        numbers = re.findall(r'\d+', text)
        for num in numbers:
            idx = int(num) - 1
            if 0 <= idx < len(default_topics):
                topic = default_topics[idx]
                if topic not in selected:
                    selected.append(topic)

        # Если не нашли цифры, ищем названия тем
        if not selected:
            text_lower = text.lower()
            for topic in default_topics:
                if topic.lower() in text_lower:
                    selected.append(topic)

        return selected[:3]  # Максимум 3 темы

    async def exit(self, user) -> dict:
        """Передаём контекст следующему стейту."""
        return {
            "from_topics": True,
            "week_started": True
        }
