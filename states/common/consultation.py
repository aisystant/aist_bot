"""
Стейт: Консультация (Слой 1 архитектуры бота).

Консультант — слой поверх всего бота, не отдельный домен.
Знает структуру бота (из Self-Knowledge) и может перенаправить в сервис (deep links).

Два пути обработки:
- "bot": вопрос о боте → FAQ или Claude + self-knowledge (без MCP, быстрее)
- "domain": вопрос о предмете → handle_question() + self-knowledge в system prompt

Progressive Refinement:
- После каждого ответа (кроме FAQ) — кнопки 👍 / 🔍 Подробнее
- 🔍 → повторный запрос с deep_search + previous_answer в контексте
- Максимум 3 раунда (initial + 2 refinements)

Вызывается из любого стейта, где allow_global содержит "consultation".
После ответа возвращается в предыдущий стейт.

Триггер: сообщение начинается с "?"
"""

import logging
from typing import Optional

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from core.registry import registry
from core.self_knowledge import get_self_knowledge, match_faq
from engines.shared.structured_lookup import structured_lookup, format_structured_context
from db.queries.qa import save_qa, get_latest_qa_id
from i18n import t

logger = logging.getLogger(__name__)

# Максимум раундов уточнения (1 = initial, 2 = first refine, 3 = max)
MAX_REFINEMENT_ROUNDS = 3


# Ключевые слова для классификации «вопрос о боте»
_BOT_KEYWORDS_RU = [
    "бот", "умеешь", "можешь", "команд", "функц", "помощ",
    "кнопк", "меню", "серви", "навиг", "как пользо", "что делает",
    "как работает бот", "возможност", "о себе", "кто ты", "расскаж",
    "знаешь о", "представ", "что ты", "твои возможн", "твои функц",
    "стек", "техноло", "база данн", "на чём напис", "на чем напис",
]
_BOT_KEYWORDS_EN = [
    "bot", "can you", "feature", "command", "help", "menu",
    "service", "navigate", "how to use", "what can", "how does the bot",
    "about yourself", "who are you", "tell me about", "your capabilit",
    "introduce", "what are you",
    "stack", "technolog", "database", "built with",
]
_BOT_KEYWORDS = _BOT_KEYWORDS_RU + _BOT_KEYWORDS_EN


def _build_feedback_keyboard(qa_id: int, refinement_round: int, lang: str) -> InlineKeyboardMarkup:
    """Собрать inline-клавиатуру с кнопками feedback."""
    row1 = [
        InlineKeyboardButton(
            text=t('consultation.btn_helpful', lang),
            callback_data=f"qa_helpful_{qa_id}"
        ),
    ]

    if refinement_round < MAX_REFINEMENT_ROUNDS:
        refine_key = 'consultation.btn_refine' if refinement_round <= 1 else 'consultation.btn_refine_more'
        row1.append(
            InlineKeyboardButton(
                text=t(refine_key, lang),
                callback_data=f"qa_refine_{qa_id}"
            )
        )

    row2 = [
        InlineKeyboardButton(
            text=t('consultation.btn_comment', lang),
            callback_data=f"qa_comment_{qa_id}"
        ),
    ]

    return InlineKeyboardMarkup(inline_keyboard=[row1, row2])


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
5. «ты/вы» = вопрос о боте, «я/мне» = вопрос о пользователе. Если пользователь путает — помоги разобраться в интерфейсе

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
        - refinement: True если это уточнение (из callback)
        - previous_answer: предыдущий ответ (для refinement)
        - refinement_round: номер раунда (2, 3)
        - comment_mode: True если ожидаем текст замечания
        - comment_qa_id: ID записи для замечания

        Returns:
        - "answered" → возврат в предыдущий стейт
        - None → остаёмся в стейте (ожидаем текст замечания)
        """
        context = context or {}

        # --- Comment mode: ожидаем текст замечания ---
        if context.get('comment_mode'):
            lang = self._get_lang(user)
            qa_id = context.get('comment_qa_id')
            # Сохраняем qa_id в current_context для handle()
            chat_id = self._get_chat_id(user)
            if chat_id and qa_id:
                from db.queries.users import update_intern
                import json
                ctx = json.loads(user.get('current_context', '{}')) if isinstance(user.get('current_context'), str) else (user.get('current_context') or {})
                ctx['qa_comment_id'] = qa_id
                await update_intern(chat_id, current_context=ctx)
            await self.send(user, t('consultation.comment_prompt', lang))
            return None  # Остаёмся в стейте, ждём текст

        question = context.get('question', '')
        lang = self._get_lang(user)
        is_refinement = context.get('refinement', False)
        previous_answer = context.get('previous_answer', '')
        refinement_round = context.get('refinement_round', 1)

        if not question:
            await self.send(user, t('consultation.no_question', lang))
            return "answered"

        # --- Триггер глубокого поиска: "ИИ ..." / "AI ..." → пропустить FAQ, сразу L3 ---
        deep_search = is_refinement  # Refinement всегда = deep search
        if not is_refinement:
            _DEEP_PREFIXES = ("ии ", "аи ", "ai ")
            q_check = question.lower()
            for prefix in _DEEP_PREFIXES:
                if q_check.startswith(prefix):
                    question = question[len(prefix):].strip()
                    deep_search = True
                    break

        try:
            # --- L1: Structured Lookup (YAML данные марафона из RAM, ~0ms) ---
            # Проверяем ДО FAQ: если есть точные данные марафона — FAQ не нужен
            structured_hit = None if deep_search else structured_lookup(question, lang)
            structured_context = format_structured_context(structured_hit, lang) if structured_hit else ""

            # --- L0: FAQ-матч (только если L1 не нашёл структурированных данных) ---
            faq_answer = None if (deep_search or structured_hit) else match_faq(question, lang)
            if faq_answer:
                response = self._format_response(faq_answer, [], lang)
                # Hint: предложить глубокий поиск
                hint = t('consultation.faq_hint', lang).format(question=question)
                response += f"\n\n{hint}"
                # FAQ — без кнопок feedback (мгновенный ответ)
                try:
                    await self.send(user, response, parse_mode="Markdown")
                except Exception:
                    await self.send(user, response)
            else:
                # Показываем индикатор обработки
                if is_refinement:
                    await self.send(user, t('consultation.refine_thinking', lang))
                else:
                    await self.send(user, t('consultation.thinking', lang))

                if deep_search:
                    # --- L3 forced: глубокий поиск через MCP ---
                    from engines.shared import handle_question

                    context_topic = self._get_current_topic(user)
                    intern_dict = self._user_to_dict(user)
                    bot_context = get_self_knowledge(lang)

                    # Refinement: inject previous answer
                    if is_refinement and previous_answer:
                        refinement_instruction = {
                            'ru': f"\n\nПРЕДЫДУЩИЙ ОТВЕТ (пользователь хочет подробнее):\n{previous_answer[:1500]}\n\nДай более детальный, глубокий ответ. Раскрой аспекты, которые не были затронуты выше.",
                            'en': f"\n\nPREVIOUS ANSWER (user wants more detail):\n{previous_answer[:1500]}\n\nGive a more detailed answer. Cover aspects not addressed above.",
                        }.get(lang, f"\n\nPREVIOUS ANSWER:\n{previous_answer[:1500]}\n\nGive more detail.")
                        bot_context += refinement_instruction
                    else:
                        # Regular deep search (ИИ prefix)
                        depth_instruction = {
                            'ru': "\n\nИНСТРУКЦИЯ ГЛУБИНЫ: Дай развёрнутый ответ, используя ВСЕ доступные фрагменты из контекста. Если в контексте есть связи между темами — покажи их. Если есть примеры — приведи. НО НЕ выдумывай то, чего в контексте нет.",
                            'en': "\n\nDEPTH INSTRUCTION: Give a comprehensive answer using ALL available context fragments. Show connections between topics if present. Cite examples from context. But DO NOT invent what is not in the context.",
                        }.get(lang, "\n\nDEPTH INSTRUCTION: Use ALL context fragments. Do not invent.")
                        bot_context += depth_instruction

                    # L1 structured data — inject even in deep search if available
                    if not is_refinement:
                        hit = structured_lookup(question, lang)
                        if hit:
                            sc = format_structured_context(hit, lang)
                            if sc:
                                bot_context = sc + "\n\n" + bot_context

                    answer, sources = await handle_question(
                        question=question,
                        intern=intern_dict,
                        context_topic=context_topic,
                        bot_context=bot_context,
                    )

                    response = self._format_response(answer, sources, lang)
                elif self._is_bot_question(question):
                    # --- L2: вопрос о боте → Claude + self-knowledge (без MCP) ---
                    answer = await self._answer_bot_question(user, question, lang)
                    response = self._format_response(answer, [], lang)
                    # Сохраняем Q&A для кнопок feedback
                    chat_id_l2 = self._get_chat_id(user)
                    if chat_id_l2:
                        try:
                            await save_qa(
                                chat_id=chat_id_l2,
                                mode=self._get_mode(user),
                                context_topic='',
                                question=question,
                                answer=answer,
                            )
                        except Exception as e:
                            logger.warning(f"L2 save_qa error: {e}")
                else:
                    # --- L3: доменный путь → MCP + Claude ---
                    from engines.shared import handle_question

                    context_topic = self._get_current_topic(user)
                    intern_dict = self._user_to_dict(user)
                    bot_context = get_self_knowledge(lang)

                    # L1 structured data → prepend to bot_context
                    if structured_context:
                        bot_context = structured_context + "\n\n" + bot_context

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

                # Отправляем ответ с кнопками feedback
                chat_id = self._get_chat_id(user)
                qa_id = await get_latest_qa_id(chat_id) if chat_id else None

                reply_markup = None
                if qa_id:
                    reply_markup = _build_feedback_keyboard(qa_id, refinement_round, lang)

                try:
                    await self.send(user, response, parse_mode="Markdown", reply_markup=reply_markup)
                except Exception as send_err:
                    logger.warning(f"Consultation markdown error, falling back to plain text: {send_err}")
                    await self.send(user, response, reply_markup=reply_markup)

        except Exception as e:
            logger.error(f"Consultation error: {e}")
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
        Обрабатываем followup вопросы и замечания.

        Returns:
        - "followup" → обрабатываем ещё один вопрос
        - "done" → возврат в предыдущий стейт
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)

        # --- Проверяем: ожидаем ли замечание? ---
        import json
        ctx = json.loads(user.get('current_context', '{}')) if isinstance(user.get('current_context'), str) else (user.get('current_context') or {})
        qa_comment_id = ctx.get('qa_comment_id')

        if qa_comment_id and text and not text.startswith('?'):
            # Сохраняем замечание
            from db.queries.qa import update_qa_comment
            from db.queries.users import update_intern
            try:
                await update_qa_comment(qa_comment_id, text)
                # Очищаем флаг
                del ctx['qa_comment_id']
                chat_id = self._get_chat_id(user)
                if chat_id:
                    await update_intern(chat_id, current_context=ctx)
                await self.send(user, t('consultation.comment_saved', lang))
            except Exception as e:
                logger.error(f"Comment save error: {e}")
                await self.send(user, t('consultation.error', lang))
            return "done"

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
