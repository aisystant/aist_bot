"""
Стейт: Консультация.

Глобальный стейт для ответа на вопросы пользователя.
Вызывается из любого стейта, где allow_global содержит "consultation".
После ответа возвращается в предыдущий стейт.

Триггер: сообщение начинается с "?"
"""

from typing import Optional

from aiogram.types import Message

from states.base import BaseState
from i18n import t


class ConsultationState(BaseState):
    """
    Стейт консультации.

    Обрабатывает вопросы пользователя через Claude + MCP.
    После ответа автоматически возвращается в предыдущий стейт.
    """

    name = "common.consultation"
    display_name = {"ru": "Консультация", "en": "Consultation"}
    # Консультация не имеет allow_global — это сам глобальный стейт

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

    def _get_mode(self, user) -> str:
        """Получить текущий режим пользователя."""
        if isinstance(user, dict):
            return user.get('mode', 'marathon')
        return getattr(user, 'mode', 'marathon')

    def _get_current_topic(self, user) -> Optional[str]:
        """Получить текущую тему для контекста."""
        if isinstance(user, dict):
            return user.get('current_topic')
        return getattr(user, 'current_topic', None)

    def _user_to_dict(self, user) -> dict:
        """Преобразовать user в dict для handle_question."""
        if isinstance(user, dict):
            return user
        # Собираем нужные поля
        return {
            'chat_id': getattr(user, 'chat_id', None),
            'name': getattr(user, 'name', None),
            'language': getattr(user, 'language', 'ru'),
            'mode': getattr(user, 'mode', 'marathon'),
            'occupation': getattr(user, 'occupation', None),
            'completed_topics': getattr(user, 'completed_topics', []),
            'current_topic_index': getattr(user, 'current_topic_index', 0),
            'complexity_level': getattr(user, 'complexity_level', 1),
        }

    async def enter(self, user, context: dict = None) -> Optional[str]:
        """
        Обрабатываем вопрос пользователя.

        Context содержит:
        - question: текст вопроса (без префикса ?)
        - previous_state: откуда пришли

        Returns:
        - "answered" → возврат в предыдущий стейт
        """
        context = context or {}
        question = context.get('question', '')
        lang = self._get_lang(user)

        if not question:
            await self.send(user, t('consultation.no_question', lang))
            return "answered"

        # Показываем индикатор обработки
        await self.send(user, f"💭 {t('consultation.thinking', lang)}")

        try:
            # Импортируем handle_question
            from engines.shared import handle_question

            # Получаем контекст темы
            context_topic = self._get_current_topic(user)
            intern_dict = self._user_to_dict(user)

            # Вызываем существующий обработчик
            answer, sources = await handle_question(
                question=question,
                intern=intern_dict,
                context_topic=context_topic,
            )

            # Форматируем ответ
            response = self._format_response(answer, sources, lang)
            await self.send(user, response, parse_mode="Markdown")

        except Exception as e:
            # Логируем ошибку и показываем сообщение
            import logging
            logging.getLogger(__name__).error(f"Consultation error: {e}")
            await self.send(user, t('consultation.error', lang))

        # Автоматический возврат в предыдущий стейт
        return "answered"

    def _format_response(self, answer: str, sources: list, lang: str) -> str:
        """Форматируем ответ с источниками."""
        response = answer

        if sources:
            # Максимум 2 источника
            sources_text = ", ".join(sources[:2])
            response += f"\n\n📚 _{t('consultation.sources', lang)}: {sources_text}_"

        return response

    async def handle(self, user, message: Message) -> Optional[str]:
        """
        Обрабатываем followup вопросы.

        Returns:
        - "followup" → обрабатываем ещё один вопрос
        - "done" → возврат в предыдущий стейт
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)

        # Если это ещё один вопрос
        if text.startswith('?'):
            question = text[1:].strip()
            if question:
                # Обрабатываем как новый вопрос
                await self.enter(user, context={'question': question})
                return "followup"

        # Любое другое сообщение — возврат
        # Сообщаем что консультация завершена
        await self.send(user, t('consultation.returning', lang))
        return "done"

    async def exit(self, user) -> dict:
        """Передаём контекст обратно."""
        return {
            "consultation_complete": True
        }
