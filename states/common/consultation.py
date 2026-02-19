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

Persistent Session:
- После ответа бот остаётся в стейте (enter() → None)
- Текст без "?" трактуется как follow-up вопрос
- Claude получает conversation history (последние 3-5 пар)
- Выход: кнопка "Завершить" / таймаут 5 мин / глобальная команда

Вызывается из любого стейта, где allow_global содержит "consultation".
Триггер: сообщение начинается с "?"
"""

import asyncio
import json
import logging
import time
from typing import Optional

from aiogram.enums import ChatAction
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from core.registry import registry
from core.self_knowledge import get_self_knowledge, match_faq
from engines.shared.structured_lookup import structured_lookup, format_structured_context
from db.queries.qa import save_qa, get_latest_qa_id
from clients.digital_twin import digital_twin
from clients.github_oauth import github_oauth
from i18n import t
from helpers.markdown_to_html import md_to_html

logger = logging.getLogger(__name__)

# Максимум раундов уточнения (1 = initial, 2 = first refine, 3 = max)
MAX_REFINEMENT_ROUNDS = 3

# Persistent session: максимум пар (user/assistant) в истории
MAX_HISTORY_PAIRS = 5
# Максимум символов на одну запись истории (обрезка длинных ответов)
MAX_HISTORY_ENTRY_CHARS = 800
# Таймаут неактивности (секунды) — авто-выход из консультации
SESSION_TIMEOUT_SEC = 300  # 5 минут


# Ключевые слова для классификации «вопрос о боте»
_BOT_KEYWORDS_RU = [
    "бот", "умеешь", "можешь", "команд", "функц", "помощ",
    "кнопк", "меню", "серви", "навиг", "как пользо", "что делает",
    "как работает бот", "возможност", "о себе", "кто ты", "расскаж",
    "представ", "твои возможн", "твои функц",
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

# --- Meta-question patterns: hardcoded rich answers (instant, no Claude API) ---
_META_PATTERNS = {
    'capabilities': {
        'patterns_ru': [
            "что ты умеешь", "что ты можешь", "что умеешь", "что можешь",
            "твои возможности", "твои функции", "что ты делаешь",
            "что бот умеет", "что бот может", "на что способен",
        ],
        'patterns_en': [
            "what can you do", "what are your capabilities", "your features",
            "what do you do", "what are you capable of",
        ],
        'answer_ru': (
            "*Что я умею:*\n\n"
            "*Обучение*\n"
            "  /learn — Марафон (14 дней) или Лента (гибкие темы)\n"
            "  /test — тест систематичности (адаптирует контент)\n"
            "  ?вопрос — консультант по системному мышлению\n\n"
            "*Организация*\n"
            "  .текст — сохранить заметку\n"
            "  /progress — статистика обучения\n"
            "  /rp /plan /report — рабочие продукты и планы\n\n"
            "*Настройки*\n"
            "  /mode — переключить режим\n"
            "  /settings — язык, профиль, подключения\n"
            "  /mydata — просмотр данных с ИИ-объяснениями\n\n"
            "_Начни с_ /mode _для выбора режима._"
        ),
        'answer_en': (
            "*What I can do:*\n\n"
            "*Learning*\n"
            "  /learn — Marathon (14 days) or Feed (flexible topics)\n"
            "  /test — systematicity assessment (adapts content)\n"
            "  ?question — systems thinking consultant\n\n"
            "*Organization*\n"
            "  .text — save a note\n"
            "  /progress — learning statistics\n"
            "  /rp /plan /report — work products and plans\n\n"
            "*Settings*\n"
            "  /mode — switch mode\n"
            "  /settings — language, profile, connections\n"
            "  /mydata — view data with AI explanations\n\n"
            "_Start with_ /mode _to choose a mode._"
        ),
    },
    'identity': {
        'patterns_ru': [
            "кто ты", "кто вы", "представься", "расскажи о себе",
            "ты кто", "что ты такое", "что это за бот",
        ],
        'patterns_en': [
            "who are you", "what are you", "introduce yourself",
            "tell me about yourself", "what is this bot",
        ],
        'answer_ru': (
            "*Я — AIST Bot* (@aist\\_me\\_bot)\n\n"
            "Бот-наставник для систематического обучения. "
            "Помогаю изучать системное мышление через структурированные программы, "
            "отвечаю на вопросы, веду заметки и отслеживаю прогресс.\n\n"
            "*Два режима обучения:*\n"
            "  /learn → *Марафон* — 14-дневная программа с теорией и практикой\n"
            "  /learn → *Лента* — выбираешь темы, получаешь дайджесты\n\n"
            "Задай вопрос: начни с `?` (например: `?Что такое системное мышление?`)\n\n"
            "_Команда_ /mode _— выбрать режим._"
        ),
        'answer_en': (
            "*I'm AIST Bot* (@aist\\_me\\_bot)\n\n"
            "A mentor bot for systematic learning. "
            "I help study systems thinking through structured programs, "
            "answer questions, keep notes, and track progress.\n\n"
            "*Two learning modes:*\n"
            "  /learn → *Marathon* — 14-day program with theory and practice\n"
            "  /learn → *Feed* — choose topics, receive digests\n\n"
            "Ask a question: start with `?` (e.g.: `?What is systems thinking?`)\n\n"
            "_Command_ /mode _— choose a mode._"
        ),
    },
}


def _match_meta_question(question: str, lang: str) -> Optional[str]:
    """Fast pattern match for meta-questions (who are you, what can you do).

    Returns formatted answer or None. ~0ms, no API calls.
    """
    q = question.lower().strip().rstrip('?!.))')
    lang_key = 'en' if lang == 'en' else 'ru'

    for meta_key, meta in _META_PATTERNS.items():
        patterns = meta.get(f'patterns_{lang_key}', []) + meta.get('patterns_ru', [])
        for pattern in patterns:
            if pattern in q:
                return meta.get(f'answer_{lang_key}', meta.get('answer_ru', ''))

    return None


def _build_feedback_keyboard(qa_id: int, refinement_round: int, lang: str) -> InlineKeyboardMarkup:
    """Собрать inline-клавиатуру с кнопками feedback + завершить сессию."""
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
        InlineKeyboardButton(
            text=t('consultation.btn_end_session', lang),
            callback_data="qa_end_session"
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
    keyboard_type = "none"

    def _keep_typing(self, chat_id: int) -> asyncio.Task:
        """Фоновая задача: продлевает typing indicator каждые 4 сек."""
        async def _loop():
            try:
                while True:
                    await asyncio.sleep(4)
                    await self.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        return asyncio.create_task(_loop())

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

    async def _detect_tier(self, user_chat_id: int) -> tuple:
        """Определяет тир обслуживания: (tier, has_github, has_dt).

        DP.ARCH.002:
        - T1 Expert: нет GitHub, нет ЦД
        - T2 Mentor: есть ЦД (без GitHub)
        - T3 Co-thinker: есть GitHub (+ personal CLAUDE.md)
        - T4 Architect: reserved (= T3 пока нет tools)
        """
        if not user_chat_id:
            return 1, False, False

        has_github = await github_oauth.is_connected(user_chat_id)
        has_dt = digital_twin.is_connected(user_chat_id)

        if has_github:
            return 3, True, has_dt
        elif has_dt:
            return 2, False, True
        return 1, False, False

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
            'interests': getattr(user, 'interests', []),
            'goals': getattr(user, 'goals', ''),
            'assessment_state': getattr(user, 'assessment_state', None),
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

    async def _answer_bot_question(self, user, question: str, lang: str, previous_answer: str = None) -> str:
        """Быстрый путь: ответ на вопрос о боте (L2).

        1. Проверить FAQ → мгновенный ответ (пропускается при refinement)
        2. Иначе → Claude с self-knowledge (без MCP-поиска)
        """
        # Попробовать FAQ (не при refinement — пользователь уже видел FAQ или L2 ответ)
        if not previous_answer:
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
            'fr': "IMPORTANT: Réponds en français.",
            'zh': "重要：请用中文回答。"
        }.get(lang, "IMPORTANT: Answer in English.")

        refinement_block = ""
        if previous_answer:
            refinement_block = {
                'ru': f"\n\nПРЕДЫДУЩИЙ ОТВЕТ (пользователь уже видел этот текст, НЕ ПОВТОРЯЙ его):\n{previous_answer[:800]}\n\nНапиши ДРУГОЙ, более развёрнутый ответ. Раскрой аспекты, которые не были затронуты выше. Приведи конкретные примеры использования.",
                'en': f"\n\nPREVIOUS ANSWER (user already saw this, DO NOT repeat it):\n{previous_answer[:800]}\n\nWrite a DIFFERENT, more detailed answer. Cover aspects not addressed above. Give concrete usage examples.",
            }.get(lang, f"\n\nPREVIOUS ANSWER (DO NOT repeat):\n{previous_answer[:800]}\n\nGive a different, more detailed answer.")

        if previous_answer:
            length_instruction = {
                'ru': "ОГРАНИЧЕНИЕ ДЛИНЫ: максимум 400 слов. Дай развёрнутый ответ с примерами и деталями.",
                'en': "LENGTH LIMIT: max 400 words. Give a detailed answer with examples.",
            }.get(lang, "LENGTH LIMIT: max 400 words. Give a detailed answer with examples.")
            max_tokens = 1600
        else:
            length_instruction = {
                'ru': "ЖЁСТКОЕ ОГРАНИЧЕНИЕ ДЛИНЫ: максимум 150 слов. Ответ — 3-5 коротких абзацев. Если информации много — выбери самое важное, остальное пропусти. Пользователь может нажать 🔍 для подробностей.",
                'en': "STRICT LENGTH LIMIT: max 150 words. 3-5 short paragraphs. Pick the most important info, user can tap 🔍 for details.",
            }.get(lang, "STRICT LENGTH LIMIT: max 150 words. 3-5 short paragraphs.")
            max_tokens = 800

        system_prompt = f"""Ты — AIST Bot, дружелюбный бот-наставник.
Отвечаешь на вопрос пользователя {name} о себе (о боте).

{lang_instruction}

ЗНАНИЯ О БОТЕ:
{self_knowledge}

{length_instruction}

ПРАВИЛА:
1. Используй информацию из знаний о боте — не выдумывай функции
2. Предлагай конкретные команды (например /learn, /test)
3. Если вопрос не о боте — вежливо перенаправь
4. «ты/вы» = вопрос о боте, «я/мне» = вопрос о пользователе
{refinement_block}
{ONTOLOGY_RULES}"""

        user_prompt = f"Вопрос: {question}" if lang == 'ru' else f"Question: {question}"
        # Bot FAQ (L2) — простая задача, Haiku достаточно
        from config import CLAUDE_MODEL_HAIKU
        answer = await claude.generate(system_prompt, user_prompt, max_tokens=max_tokens, model=CLAUDE_MODEL_HAIKU)
        return answer or t('consultation.error', lang)

    async def _load_session_context(self, user) -> dict:
        """Загрузить consultation session context из current_context в DB."""
        raw = user.get('current_context') if isinstance(user, dict) else getattr(user, 'current_context', None)
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        return raw or {}

    async def _save_session_context(self, chat_id: int, ctx: dict):
        """Сохранить consultation session context в DB."""
        from db.queries.users import update_intern
        await update_intern(chat_id, current_context=ctx)

    def _append_history(self, ctx: dict, question: str, answer: str) -> dict:
        """Добавить пару (вопрос, ответ) в conversation history."""
        history = ctx.get('consultation_history', [])
        history.append({
            'q': question[:MAX_HISTORY_ENTRY_CHARS],
            'a': answer[:MAX_HISTORY_ENTRY_CHARS],
        })
        # Оставляем последние MAX_HISTORY_PAIRS пар
        if len(history) > MAX_HISTORY_PAIRS:
            history = history[-MAX_HISTORY_PAIRS:]
        ctx['consultation_history'] = history
        ctx['consultation_last_activity'] = time.time()
        return ctx

    def _build_history_messages(self, ctx: dict, current_question: str) -> list:
        """Собрать messages[] для Claude из conversation history."""
        messages = []
        history = ctx.get('consultation_history', [])
        for pair in history:
            messages.append({"role": "user", "content": pair['q']})
            messages.append({"role": "assistant", "content": pair['a']})
        # Текущий вопрос — последнее user-сообщение
        messages.append({"role": "user", "content": current_question})
        return messages

    def _clear_session(self, ctx: dict) -> dict:
        """Очистить consultation session данные из context."""
        ctx.pop('consultation_history', None)
        ctx.pop('consultation_last_activity', None)
        ctx.pop('qa_comment_id', None)
        return ctx

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
        - None → остаёмся в стейте (persistent session)
        - "done" → доступ запрещён → возврат
        """
        context = context or {}

        # --- Проверка доступа (подписка/триал) ---
        chat_id = self._get_chat_id(user)
        if chat_id:
            from core.access import access_layer
            if not await access_layer.has_access(chat_id, 'consultation'):
                lang = self._get_lang(user)
                text, kb = await access_layer.get_paywall('consultation', lang)
                await self.send(user, text, reply_markup=kb)
                return "done"

        # --- Comment mode: ожидаем текст замечания ---
        if context.get('comment_mode'):
            lang = self._get_lang(user)
            qa_id = context.get('comment_qa_id')
            # Сохраняем qa_id в current_context для handle()
            chat_id = self._get_chat_id(user)
            if chat_id and qa_id:
                ctx = await self._load_session_context(user)
                ctx['qa_comment_id'] = qa_id
                await self._save_session_context(chat_id, ctx)
            await self.send(user, t('consultation.comment_prompt', lang))
            return None  # Остаёмся в стейте, ждём текст

        question = context.get('question', '')
        lang = self._get_lang(user)
        is_refinement = context.get('refinement', False)
        previous_answer = context.get('previous_answer', '')
        refinement_round = context.get('refinement_round', 1)

        if not question:
            await self.send(user, t('consultation.no_question', lang))
            return None  # Остаёмся — ждём вопрос

        # Загружаем session context для history
        session_ctx = await self._load_session_context(user)
        _answer_for_history = ""  # Трекинг ответа для записи в history

        # --- Meta-question fast path: "кто ты?", "что умеешь?" → instant rich response ---
        if not is_refinement:
            meta_answer = _match_meta_question(question, lang)
            if meta_answer:
                logger.info(f"[Consultation] Meta-question match: '{question[:40]}' → instant response")
                reply_markup = None
                chat_id_meta = self._get_chat_id(user)
                if chat_id_meta:
                    try:
                        qa_id = await save_qa(
                            chat_id=chat_id_meta,
                            mode=self._get_mode(user),
                            context_topic='',
                            question=question,
                            answer=meta_answer,
                        )
                        if qa_id:
                            reply_markup = _build_feedback_keyboard(qa_id, 1, lang)
                    except Exception as e:
                        logger.warning(f"Meta FAQ save_qa error: {e}")
                await self.send(user, md_to_html(meta_answer), parse_mode="HTML", reply_markup=reply_markup)
                # Сохраняем в history + остаёмся в стейте
                self._append_history(session_ctx, question, meta_answer)
                await self._save_session_context(chat_id, session_ctx)
                logger.info(f"[Consultation] Persistent session: staying after meta-answer")
                return None

        # --- Триггер глубокого поиска: "ИИ ..." / "AI ..." → пропустить FAQ, сразу L3 ---
        # Refinement: deep search только для доменных вопросов (L3).
        # Для bot-вопросов refinement → L2 (Claude + self-knowledge, без MCP).
        is_bot_q = self._is_bot_question(question)
        deep_search = is_refinement and not is_bot_q
        if not is_refinement:
            _DEEP_PREFIXES = ("ии ", "аи ", "ai ")
            q_check = question.lower()
            for prefix in _DEEP_PREFIXES:
                if q_check.startswith(prefix):
                    question = question[len(prefix):].strip()
                    deep_search = True
                    break

        typing_task = None
        try:
            # --- L1: Structured Lookup (YAML данные марафона из RAM, ~0ms) ---
            # Проверяем ДО FAQ: если есть точные данные марафона — FAQ не нужен
            structured_hit = None if deep_search else structured_lookup(question, lang)
            structured_context = format_structured_context(structured_hit, lang) if structured_hit else ""

            # --- L0: FAQ-матч (только если L1 не нашёл структурированных данных) ---
            faq_answer = None if (deep_search or structured_hit or is_refinement) else match_faq(question, lang)
            if faq_answer:
                _answer_for_history = faq_answer
                response = self._format_response(faq_answer, [], lang)
                # Hint + кнопка «Подробнее» для глубокого поиска через ИИ
                hint = t('consultation.faq_hint', lang)
                response += f"\n\n{hint}"
                # Сохраняем FAQ в qa_log для кнопки refine
                reply_markup = None
                chat_id_faq = self._get_chat_id(user)
                if chat_id_faq:
                    try:
                        qa_id = await save_qa(
                            chat_id=chat_id_faq,
                            mode=self._get_mode(user),
                            context_topic='',
                            question=question,
                            answer=faq_answer,
                        )
                        if qa_id:
                            reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(
                                    text=t('consultation.btn_refine', lang),
                                    callback_data=f"qa_refine_{qa_id}"
                                )
                            ]])
                    except Exception as e:
                        logger.warning(f"FAQ save_qa error: {e}")
                await self.send(user, md_to_html(response), parse_mode="HTML", reply_markup=reply_markup)
            else:
                # Показываем индикатор обработки
                if is_refinement:
                    await self.send(user, t('consultation.refine_thinking', lang))
                else:
                    await self.send(user, t('consultation.thinking', lang))

                # Продлеваем typing на время тяжёлой операции (>5 сек)
                typing_task = self._keep_typing(chat_id)

                if is_bot_q and not deep_search:
                    # --- L2: вопрос о боте → Claude + self-knowledge (без MCP) ---
                    answer = await self._answer_bot_question(
                        user, question, lang,
                        previous_answer=previous_answer if is_refinement else None,
                    )
                    _answer_for_history = answer
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
                    # --- L3: предметный вопрос → tool_use для ВСЕХ тиров (T1-T4) ---
                    context_topic = self._get_current_topic(user)
                    intern_dict = self._user_to_dict(user)
                    bot_context = get_self_knowledge(lang)

                    # L1 structured data → prepend to bot_context
                    if structured_context:
                        bot_context = structured_context + "\n\n" + bot_context

                    # Refinement: inject previous answer
                    if is_refinement and previous_answer:
                        refinement_instruction = {
                            'ru': f"\n\nПРЕДЫДУЩИЙ ОТВЕТ (пользователь хочет подробнее):\n{previous_answer[:800]}\n\nДай более детальный, глубокий ответ. Раскрой аспекты, которые не были затронуты выше.",
                            'en': f"\n\nPREVIOUS ANSWER (user wants more detail):\n{previous_answer[:800]}\n\nGive a more detailed answer. Cover aspects not addressed above.",
                        }.get(lang, f"\n\nPREVIOUS ANSWER:\n{previous_answer[:800]}\n\nGive more detail.")
                        bot_context += refinement_instruction
                    elif deep_search:
                        depth_instruction = {
                            'ru': "\n\nИНСТРУКЦИЯ ГЛУБИНЫ: Дай развёрнутый ответ, используя ВСЕ доступные фрагменты из контекста. Если в контексте есть связи между темами — покажи их. Если есть примеры — приведи. НО НЕ выдумывай то, чего в контексте нет.",
                            'en': "\n\nDEPTH INSTRUCTION: Give a comprehensive answer using ALL available context fragments. Show connections between topics if present. Cite examples from context. But DO NOT invent what is not in the context.",
                        }.get(lang, "\n\nDEPTH INSTRUCTION: Use ALL context fragments. Do not invent.")
                        bot_context += depth_instruction

                    # L1 structured data for deep search
                    if deep_search and not is_refinement and not structured_context:
                        hit = structured_lookup(question, lang)
                        if hit:
                            sc = format_structured_context(hit, lang)
                            if sc:
                                bot_context = sc + "\n\n" + bot_context

                    # Определяем тир (DP.ARCH.002)
                    user_chat_id = self._get_chat_id(user)
                    tier, has_github, has_dt = await self._detect_tier(user_chat_id)

                    # Proactive DT injection: detect personal query → fetch DT data
                    if has_dt:
                        from engines.shared.personal_detector import detect_personal_query, fetch_dt_context
                        dt_paths = detect_personal_query(question)
                        if dt_paths:
                            dt_context = await fetch_dt_context(user_chat_id, dt_paths)
                            if dt_context:
                                bot_context = dt_context + "\n\n" + bot_context

                    from engines.shared import handle_question_with_tools
                    from engines.shared.consultation_tools import get_personal_claude_md

                    personal_claude = ""
                    if has_github:
                        personal_claude = await get_personal_claude_md(user_chat_id)

                    # Conversation history → multi-turn messages
                    history_messages = self._build_history_messages(session_ctx, question) if session_ctx.get('consultation_history') else None

                    answer, sources = await handle_question_with_tools(
                        question=question,
                        intern=intern_dict,
                        context_topic=context_topic,
                        bot_context=bot_context,
                        has_digital_twin=has_dt,
                        personal_claude_md=personal_claude,
                        tier=tier,
                        is_refinement=is_refinement,
                        conversation_messages=history_messages,
                    )
                    logger.info(f"Consultation: T{tier} tool_use path for user {user_chat_id}")
                    _answer_for_history = answer

                    response = self._format_response(answer, sources, lang)

                typing_task.cancel()

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

                await self.send(user, md_to_html(response), parse_mode="HTML", reply_markup=reply_markup)

        except Exception as e:
            if typing_task:
                typing_task.cancel()
            logger.error(f"Consultation error: {e}", exc_info=True)
            await self.send(user, t('consultation.error', lang))
            return None

        # Сохраняем ответ в conversation history
        try:
            if question and _answer_for_history:
                self._append_history(session_ctx, question, _answer_for_history)
                await self._save_session_context(chat_id, session_ctx)
                logger.info(f"[Consultation] History saved, {len(session_ctx.get('consultation_history', []))} pairs")
        except Exception as e:
            logger.warning(f"Consultation history save error: {e}")

        # Persistent session: остаёмся в стейте
        return None

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
        - "followup" → обрабатываем ещё один вопрос (→ _same)
        - "done" → возврат в предыдущий стейт (→ _previous)
        - None → остаёмся (ожидаем текст замечания)
        """
        text = (message.text or "").strip()
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        # --- Проверяем: ожидаем ли замечание? ---
        ctx = await self._load_session_context(user)
        qa_comment_id = ctx.get('qa_comment_id')

        if qa_comment_id and text and not text.startswith('?'):
            # Сохраняем замечание
            from db.queries.qa import update_qa_comment
            try:
                await update_qa_comment(qa_comment_id, text)
                # Очищаем флаг
                del ctx['qa_comment_id']
                if chat_id:
                    await self._save_session_context(chat_id, ctx)
                # Подтверждение + подсказка с кнопкой "Завершить"
                end_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=t('consultation.btn_end_session', lang),
                        callback_data="qa_end_session"
                    )
                ]])
                msg = t('consultation.comment_saved', lang) + "\n\n" + t('consultation.session_hint', lang)
                await self.send(user, msg, reply_markup=end_kb)
            except Exception as e:
                logger.error(f"Comment save error: {e}")
                await self.send(user, t('consultation.error', lang))
            return None  # Остаёмся в сессии после замечания

        # --- Проверка таймаута (5 мин неактивности) ---
        last_activity = ctx.get('consultation_last_activity', 0)
        if last_activity and (time.time() - last_activity) > SESSION_TIMEOUT_SEC:
            logger.info(f"[Consultation] Session timeout for chat {chat_id}")
            await self._end_session(user, ctx, lang)
            return "done"

        # --- Вопрос с "?" → явный новый вопрос ---
        if text.startswith('?'):
            question = text[1:].strip()
            if question:
                await self.enter(user, context={'question': question})
                return "followup"

        # --- Текст без "?" (≥3 символов) → follow-up вопрос ---
        if len(text) >= 3:
            await self.enter(user, context={'question': text})
            return "followup"

        # Слишком короткий текст — подсказка
        await self.send(user, t('consultation.session_hint', lang))
        return None

    async def _end_session(self, user, ctx: dict, lang: str):
        """Завершить consultation session: очистка history, прощание."""
        chat_id = self._get_chat_id(user)
        self._clear_session(ctx)
        if chat_id:
            await self._save_session_context(chat_id, ctx)
        await self.send(user, t('consultation.session_ended', lang))
        logger.info(f"[Consultation] Session ended for chat {chat_id}")

    async def exit(self, user) -> dict:
        """Передаём контекст обратно + очищаем session history."""
        chat_id = self._get_chat_id(user)
        if chat_id:
            try:
                ctx = await self._load_session_context(user)
                self._clear_session(ctx)
                await self._save_session_context(chat_id, ctx)
            except Exception as e:
                logger.warning(f"Consultation exit cleanup error: {e}")
        return {
            "consultation_complete": True
        }
