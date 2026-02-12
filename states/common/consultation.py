"""
Стейт: Консультация (Слой 1 архитектуры бота).

Консультант — слой поверх всего бота, не отдельный домен.
Знает структуру бота (из Self-Knowledge) и может перенаправить в сервис (deep links).

Два пути обработки:
- "bot": вопрос о боте → FAQ или Claude + self-knowledge (без MCP, быстрее)
- "domain": вопрос о предмете → handle_question() + self-knowledge в system prompt

Вызывается из любого стейта, где allow_global содержит "consultation".
После ответа возвращается в предыдущий стейт.

Триггер: сообщение начинается с "?"
"""

from typing import Optional

from aiogram.types import Message

from states.base import BaseState
from core.registry import registry
from core.self_knowledge import get_self_knowledge, match_faq
from i18n import t


# Ключевые слова для классификации «вопрос о боте»
_BOT_KEYWORDS_RU = [
    "бот", "умеешь", "можешь", "команд", "функц", "помощ",
    "кнопк", "меню", "серви", "навиг", "как пользо", "что делает",
    "как работает бот", "возможност", "о себе", "кто ты", "расскаж",
    "знаешь о", "представ", "что ты", "твои возможн", "твои функц",
]
_BOT_KEYWORDS_EN = [
    "bot", "can you", "feature", "command", "help", "menu",
    "service", "navigate", "how to use", "what can", "how does the bot",
    "about yourself", "who are you", "tell me about", "your capabilit",
    "introduce", "what are you",
]
_BOT_KEYWORDS = _BOT_KEYWORDS_RU + _BOT_KEYWORDS_EN


class ConsultationState(BaseState):
    """
    Стейт консультации.

    Обрабатывает вопросы пользователя через два пути:
    - bot: вопросы о боте (FAQ + Claude с self-knowledge)
    - domain: предметные вопросы (MCP + Claude)

    После ответа автоматически возвращается в предыдущий стейт.
    """

    name = "common.consultation"
    display_name = {"ru": "Консультация", "en": "Consultation", "es": "Consulta", "fr": "Consultation"}

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

    def _is_bot_question(self, question: str) -> bool:
        """Классифицировать: вопрос о боте или о домене?"""
        q = question.lower()
        return (
            any(kw in q for kw in _BOT_KEYWORDS)
            or self._detect_service_intent(question) is not None
        )

    def _detect_service_intent(self, question: str) -> Optional[str]:
        """Определяет, относится ли вопрос к конкретному сервису.

        Если да — возвращает service_id для deep link.
        """
        q = question.lower()
        keyword_map = {
            "learning": ["учи", "урок", "тем", "марафон", "лент", "learn", "lesson", "marathon", "feed"],
            "plans": ["план", "рп", "отчет", "report", "plan"],
            "notes": ["замет", "note"],
            "progress": ["прогресс", "статистик", "progress"],
            "assessment": ["тест", "оценк", "assessment", "test"],
            "settings": ["настрой", "setting", "язык", "language"],
        }

        for service_id, keywords in keyword_map.items():
            if any(kw in q for kw in keywords):
                if registry.get(service_id):
                    return service_id

        return None

    async def _answer_bot_question(self, user, question: str, lang: str) -> str:
        """Быстрый путь: ответ на вопрос о боте.

        1. Проверить FAQ → мгновенный ответ
        2. Иначе → Claude с self-knowledge (без MCP-поиска)
        """
        # Попробовать FAQ
        faq_answer = match_faq(question, lang)
        if faq_answer:
            return faq_answer

        # Claude с self-knowledge в system prompt
        from clients import claude
        from config import ONTOLOGY_RULES

        name = self._user_to_dict(user).get('name', '')
        self_knowledge = get_self_knowledge(lang)

        lang_instruction = {
            'ru': "ВАЖНО: Отвечай на русском языке.",
            'en': "IMPORTANT: Answer in English.",
            'es': "IMPORTANTE: Responde en español.",
            'fr': "IMPORTANT: Réponds en français."
        }.get(lang, "IMPORTANT: Answer in English.")

        system_prompt = f"""Ты — AIST Bot, дружелюбный бот-наставник.
Отвечаешь на вопрос пользователя {name} о себе (о боте).

{lang_instruction}

ЗНАНИЯ О БОТЕ:
{self_knowledge}

ПРАВИЛА:
1. Отвечай кратко и по существу (2-4 абзаца)
2. Используй информацию из знаний о боте — не выдумывай функции
3. Предлагай конкретные команды (например /learn, /test)
4. Если вопрос не о боте — вежливо перенаправь

{ONTOLOGY_RULES}"""

        user_prompt = f"Вопрос: {question}" if lang == 'ru' else f"Question: {question}"
        answer = await claude.generate(system_prompt, user_prompt)
        return answer or t('consultation.error', lang)

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
            if self._is_bot_question(question):
                # --- Быстрый путь: вопрос о боте ---
                answer = await self._answer_bot_question(user, question, lang)
                response = self._format_response(answer, [], lang)
            else:
                # --- Доменный путь: MCP + Claude ---
                from engines.shared import handle_question

                context_topic = self._get_current_topic(user)
                intern_dict = self._user_to_dict(user)
                bot_context = get_self_knowledge(lang)

                answer, sources = await handle_question(
                    question=question,
                    intern=intern_dict,
                    context_topic=context_topic,
                    bot_context=bot_context,
                )

                response = self._format_response(answer, sources, lang)

            # Добавляем deep link если вопрос относится к сервису
            service_id = self._detect_service_intent(question)
            if service_id:
                service = registry.get(service_id)
                if service and service.command:
                    response += f"\n\n{service.icon} {t('consultation.try_service', lang)}: {service.command}"

            await self.send(user, response, parse_mode="Markdown")

        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Consultation error: {e}")
            await self.send(user, t('consultation.error', lang))

        # Автоматический возврат в предыдущий стейт
        return "answered"

    def _format_response(self, answer: str, sources: list, lang: str) -> str:
        """Форматируем ответ с источниками."""
        response = answer

        if sources:
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
                await self.enter(user, context={'question': question})
                return "followup"

        # Любое другое сообщение — возврат
        await self.send(user, t('consultation.returning', lang))
        return "done"

    async def exit(self, user) -> dict:
        """Передаём контекст обратно."""
        return {
            "consultation_complete": True
        }
