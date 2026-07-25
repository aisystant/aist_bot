from __future__ import annotations

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
- Выход: кнопка "Завершить" / таймаут 5 мин (только без нового вопроса) / глобальная команда

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
from clients.gateway_mcp import gateway_mcp
from clients.github_oauth import github_oauth
from i18n import t
from helpers.message_split import prepare_html_parts

logger = logging.getLogger(__name__)

# Максимум раундов уточнения (1 = initial, 2 = first refine, 3 = max)
MAX_REFINEMENT_ROUNDS = 3

# --- Role routing patterns (DP.D.044) ---
_NAVIGATOR_PATTERNS = [
    # SS.1: С чего начать / выбор пути
    "с чего начать", "с чего мне начать", "что мне изучать", "какую программу",
    "куда пойти", "что выбрать", "какой курс", "программа обучения",
    "не знаю с чего", "подскажи путь", "что посоветуешь изучать",
    "как начать учиться", "помоги выбрать", "что дальше учить",
    "какой путь", "порекомендуй программу", "подскажи программу",
    # SS.3: Мемы (учебные барьеры)
    "нет времени", "не хватает времени", "не получается учиться",
    "сбиваюсь", "бросаю", "не могу учиться", "мешает учиться",
    "нужно идеально", "потом начну", "сначала пойму",
    "не мне это", "поздно учиться", "нет способностей",
    # SS.4: Помидорки (ритм обучения)
    "сколько помидорок", "сколько учиться", "как спланировать неделю",
    "помоги с ритмом", "ритм обучения", "план на неделю",
    "сколько времени уделять", "расписание обучения",
    # SS.5: Итоги
    "итоги недели", "итоги", "как у меня дела", "как я продвинулся",
    "мой прогресс за неделю", "подведи итоги",
    # SS.6: Зачем
    "зачем это учить", "зачем мне это", "какой смысл", "не понимаю зачем",
    "для чего это нужно", "почему это важно",
]

_DIAGNOSTICIAN_PATTERNS = [
    "протестируй меня", "протестируй", "какая у меня ступень",
    "определи мою ступень", "на какой я ступени", "моя ступень",
    "оцени мой уровень", "диагностика", "тестирование ступени",
]

# WP-498 Ф5: Наставник (MIM.R.001 Режим 2) — always-on 1:1 диагноз+рекомендация.
# Черновик лексики из WP-498.md варианта B (25.07), + ближайшие естественные
# вариации написания (без запятой / без окончания). Осознанно НЕ расширено
# дальше — более широкий список не пройден пилотом, риск ложного
# срабатывания уже отмечен как повышенный (см. WP-498.md, вариант B, минус).
_MENTOR_PATTERNS = [
    "застрял", "застряла",
    "не получается",
    "не знаю, что делать", "не знаю что делать",
    "упал мотивацией", "упала мотивация", "потерял мотивацию", "нет мотивации",
    "в тупике",
]


# WP-156: Explicit role prefixes — user can address a role directly
# WP-498 Ф5: "наставник" добавлен по тому же образцу (25.07).
_ROLE_PREFIXES = {
    'navigator': ['навигатор', 'navigator'],
    'diagnostician': ['диагност', 'diagnostician'],
    'mentor': ['наставник', 'mentor'],
}


def _detect_role(question: str) -> Optional[str]:
    """Определяет, нужна ли смена роли (DP.D.044).

    Priority:
    1. Explicit prefix: "Навигатор, ..." / "Диагност, ..." / "Наставник, ..."
    2. Pattern match from question content — diagnostician → navigator → mentor.
       Mentor patterns are checked last (lowest priority): WP-498.md вариант B
       explicitly flags higher false-positive risk for mentor lexicon (широкое
       полномочие 4-компонентной связки), а ошибочный роутинг сюда дороже, чем
       в узкую роль Навигатора/Диагноста — поэтому более специфичные паттерны
       двух существующих ролей должны выигрывать при конфликте.

    Returns:
        "navigator" | "diagnostician" | "mentor" | None (Консультант по умолчанию)
    """
    q = question.lower().strip()

    # 1. Explicit role prefix (highest priority)
    for role, prefixes in _ROLE_PREFIXES.items():
        for prefix in prefixes:
            if q.startswith(prefix):
                return role

    # 2. Content pattern matching
    for pattern in _DIAGNOSTICIAN_PATTERNS:
        if pattern in q:
            return "diagnostician"

    for pattern in _NAVIGATOR_PATTERNS:
        if pattern in q:
            return "navigator"

    for pattern in _MENTOR_PATTERNS:
        if pattern in q:
            return "mentor"

    return None

# Persistent session: максимум пар (user/assistant) в истории
MAX_HISTORY_PAIRS = 5
# Максимум символов на одну запись истории (обрезка длинных ответов)
MAX_HISTORY_ENTRY_CHARS = 800
# Таймаут неактивности (секунды) — авто-выход из консультации
SESSION_TIMEOUT_SEC = 300  # 5 минут





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
            "*Развитие*\n"
            "  /learn — Марафон (14 дней) или Лента (гибкие темы)\n"
            "  /test — тест систематичности (адаптирует контент)\n"
            "  ?вопрос — консультант по системному мышлению\n\n"
            "*Организация*\n"
            "  .текст — сохранить заметку\n"
            "  /progress — статистика развития\n"
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
            "Твой личный наставник в систематическом развитии. "
            "Помогаю разобраться в системном мышлении, выстроить привычку учиться "
            "и отслеживать прогресс — всё в одном боте.\n\n"
            "*Что я умею:*\n"
            "  /learn — Марафон (14 дней) или Лента (дайджесты по темам)\n"
            "  /test — Тест систематичности — определит твоё состояние\n"
            "  `?` — Задай вопрос консультанту (например: `?Что такое системное мышление?`)\n"
            "  /progress — Статистика развития\n"
            "  /profile — Твой профиль и цели\n"
            "  /rp /plan /report — Планы и рабочие продукты\n\n"
            "*Уровни доступа:*\n"
            "  🟢 T0/T1 — Марафон, тест, прогресс — бесплатно\n"
            "  📘 T2 — Лента, консультант, заметки, планы (подписка «Инженерия интеллекта»)\n"
            "  🧬 T3 — Персональные рекомендации (цифровой двойник)\n"
            "  🚀 T4 — Локальный экзокортекс, Claude Code + агенты\n\n"
            "_Начни с_ /mode _— выбрать режим._"
        ),
        'answer_en': (
            "*I'm AIST Bot* (@aist\\_me\\_bot)\n\n"
            "Your personal mentor for systematic learning and development. "
            "I help you understand systems thinking, build a learning habit, "
            "and track your progress — all in one bot.\n\n"
            "*What I can do:*\n"
            "  /learn — Marathon (14 days) or Feed (topic digests)\n"
            "  /test — Systematicity test — identifies your state\n"
            "  `?` — Ask the consultant (e.g.: `?What is systems thinking?`)\n"
            "  /progress — Learning statistics\n"
            "  /profile — Your profile and goals\n"
            "  /rp /plan /report — Plans and work products\n\n"
            "*Access tiers:*\n"
            "  🟢 T0/T1 — Marathon, test, progress — free\n"
            "  📘 T2 — Feed, consultant, notes, plans (subscription «Инженерия интеллекта»)\n"
            "  🧬 T3 — Personal recommendations (digital twin)\n"
            "  🚀 T4 — Local exocortex, Claude Code + agents\n\n"
            "_Start with_ /mode _— choose a mode._"
        ),
    },
}


def _match_meta_question(question: str, lang: str) -> Optional[str]:
    """Fast pattern match for meta-questions (who are you, what can you do).

    Returns formatted answer or None. ~0ms, no API calls.
    """
    q = question.lower().strip().rstrip('?!.))')
    lang_key = 'ru' if lang == 'ru' else 'en'

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
        has_dt = gateway_mcp.is_connected(user_chat_id)

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
            'marathon_status': getattr(user, 'marathon_status', 'not_started'),
            'marathon_start_date': getattr(user, 'marathon_start_date', None),
            'active_days_streak': getattr(user, 'active_days_streak', 0),
            'active_days_total': getattr(user, 'active_days_total', 0),
            'longest_streak': getattr(user, 'longest_streak', 0),
            'last_active_date': getattr(user, 'last_active_date', None),
            'feed_status': getattr(user, 'feed_status', 'not_started'),
            'created_at': getattr(user, 'created_at', None),
            'onboarding_completed': getattr(user, 'onboarding_completed', False),
        }

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
        logger.info(f"[Consultation] enter: chat_id={chat_id}, force_role={context.get('force_role')}, question={bool(context.get('question'))}")
        if chat_id:
            from core.access import access_layer
            has_access = await access_layer.has_access(chat_id, 'consultation')
            logger.info(f"[Consultation] access check for chat_id={chat_id}: {has_access}")
            if not has_access:
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

        # WP-156: Explicit role entry (/navigator) — save in session, show greeting
        force_role = context.get('force_role')
        if force_role and force_role in ('navigator', 'diagnostician'):
            chat_id_fr = self._get_chat_id(user)
            if chat_id_fr:
                session_ctx_fr = await self._load_session_context(user)
                session_ctx_fr['force_role'] = force_role
                self._clear_session(session_ctx_fr)  # New session for role
                session_ctx_fr['consultation_last_activity'] = time.time()
                await self._save_session_context(chat_id_fr, session_ctx_fr)

            if not question:
                # No question yet — show role greeting and wait
                from engines.shared.consultation_tools import ROLE_TRANSITION
                transition = ROLE_TRANSITION.get(force_role, {})
                greeting = transition.get(lang, transition.get("ru", ""))
                if greeting:
                    await self.send(user, greeting, parse_mode="Markdown")
                role_hint = {
                    'navigator': {
                        'ru': "Задай вопрос, и я помогу выбрать путь развития. Например:\n• _С чего начать?_\n• _Какую программу выбрать?_\n• _Как спланировать неделю?_",
                        'en': "Ask a question and I'll help you choose a learning path. For example:\n• _Where to start?_\n• _Which program to choose?_\n• _How to plan my week?_",
                    },
                    'diagnostician': {
                        'ru': "Я помогу определить твою ступень. Задай вопрос или скажи:\n• _Какая у меня ступень?_\n• _Протестируй меня_",
                        'en': "I'll help determine your level. Ask a question or say:\n• _What's my level?_\n• _Test me_",
                    },
                }
                hint = role_hint.get(force_role, {}).get(lang, role_hint.get(force_role, {}).get('ru', ''))
                if hint:
                    await self.send(user, hint, parse_mode="Markdown")
                return None  # Stay — waiting for question

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
                logger.info(f"[Consultation] Meta-question match: len={len(question)} → instant response")
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
                parts = prepare_html_parts(meta_answer)
                for i, part in enumerate(parts):
                    is_last = (i == len(parts) - 1)
                    kb = reply_markup if is_last else None
                    await self.send(user, part, parse_mode="HTML", reply_markup=kb)
                # Сохраняем в history + остаёмся в стейте
                self._append_history(session_ctx, question, meta_answer)
                await self._save_session_context(chat_id, session_ctx)
                logger.info(f"[Consultation] Persistent session: staying after meta-answer")
                return None

        # --- Триггер глубокого поиска: "ИИ ..." / "AI ..." → пропустить FAQ, сразу L3 ---
        deep_search = is_refinement
        if not is_refinement:
            _DEEP_PREFIXES = ("ии ", "аи ", "ai ")
            q_check = question.lower()
            for prefix in _DEEP_PREFIXES:
                if q_check.startswith(prefix):
                    question = question[len(prefix):].strip()
                    deep_search = True
                    break

        # DP.D.044: Early role detection — перед FAQ и structured lookup
        # Если вопрос для Навигатора/Диагноста → пропустить FAQ, идти в L3
        _early_role = _detect_role(question) if not is_refinement else None
        _skip_faq = bool(_early_role)  # Отдельный флаг, не перегружаем deep_search

        typing_task = None
        try:
            # --- L0: Structured Lookup (YAML данные марафона из RAM, ~0ms) ---
            # Проверяем ДО FAQ: если есть точные данные марафона — FAQ не нужен
            structured_hit = None if (deep_search or _skip_faq) else structured_lookup(question, lang)
            structured_context = format_structured_context(structured_hit, lang) if structured_hit else ""

            # --- L1: FAQ-матч (только если L0 не нашёл структурированных данных) ---
            faq_answer = None if (deep_search or structured_hit or is_refinement or _skip_faq) else match_faq(question, lang)
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
                parts = prepare_html_parts(response)
                for i, part in enumerate(parts):
                    is_last = (i == len(parts) - 1)
                    kb = reply_markup if is_last else None
                    await self.send(user, part, parse_mode="HTML", reply_markup=kb)
            else:
                # Показываем индикатор обработки
                if is_refinement:
                    await self.send(user, t('consultation.refine_thinking', lang))
                else:
                    await self.send(user, t('consultation.thinking', lang))

                # Продлеваем typing на время тяжёлой операции (>5 сек)
                typing_task = self._keep_typing(chat_id)

                # --- L3: единый путь → tool_use для ВСЕХ вопросов (T1-T4) ---
                # LLM сам решает через tools: искать в knowledge base или в bot_info
                context_topic = self._get_current_topic(user)
                intern_dict = self._user_to_dict(user)
                bot_context = get_self_knowledge(lang)

                # L1 structured data → prepend to bot_context
                if structured_context:
                    bot_context = structured_context + "\n\n" + bot_context

                # Refinement: inject previous answer
                if is_refinement and previous_answer:
                    # Short previous_answer (< 400 chars) means it was a FAQ hit —
                    # it may not be related to the question. Use direct instruction
                    # instead of "expand aspects" which is meaningless in that case.
                    if len(previous_answer) < 400:
                        refinement_instruction = {
                            'ru': f"\n\nПРЕДЫДУЩИЙ ОТВЕТ БОТА:\n{previous_answer[:800]}\n\nПользователь хочет узнать подробнее. Дай конкретный практический ответ на его вопрос, используя найденную информацию. Если предыдущий ответ не отвечал на вопрос напрямую — сосредоточься на точном ответе.",
                            'en': f"\n\nPREVIOUS BOT ANSWER:\n{previous_answer[:800]}\n\nThe user wants more detail. Give a concrete practical answer to their question using found information. If the previous answer did not directly address the question — focus on answering it precisely.",
                        }.get(lang, f"\n\nPREVIOUS ANSWER:\n{previous_answer[:800]}\n\nGive a precise practical answer to the user's question.")
                    else:
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

                # UITier для контекста (anti-hallucination: Claude должен знать точный тир)
                from core.tier_detector import detect_ui_tier
                ui_tier = await detect_ui_tier(user_chat_id) if user_chat_id else -1

                # Proactive DT injection: detect personal query → fetch DT data
                if has_dt:
                    from engines.shared.personal_detector import detect_personal_query, fetch_dt_context
                    dt_paths = detect_personal_query(question)
                    if dt_paths:
                        dt_context = await fetch_dt_context(user_chat_id, dt_paths)
                        if dt_context:
                            bot_context = dt_context + "\n\n" + bot_context

                # C6: Goal → Program matching (DP.ARCH.002 § 12.8)
                user_goals = intern_dict.get('goals', '') or ''
                user_interests = intern_dict.get('interests', '') or ''
                if user_goals or user_interests:
                    from config.conversion import match_goals_to_program, PROGRAM_NAMES
                    from config.settings import PLATFORM_URLS
                    matched_program = match_goals_to_program(user_goals, user_interests)
                    if matched_program:
                        pname = PROGRAM_NAMES[matched_program].get(lang, PROGRAM_NAMES[matched_program]["ru"])
                        purl = PLATFORM_URLS[matched_program]
                        goal_hint = {
                            'ru': f"\n\nПЕРСОНАЛЬНАЯ РЕКОМЕНДАЦИЯ: На основе целей пользователя лучше всего подходит программа «{pname}»: {purl}. Упомяни это, если вопрос про развитие или «что дальше».",
                            'en': f"\n\nPERSONAL RECOMMENDATION: Based on user goals, the best-fit program is «{pname}»: {purl}. Mention this if the question is about development, learning, or 'what's next'.",
                        }.get(lang, '')
                        if goal_hint:
                            bot_context += goal_hint

                from engines.shared import handle_question_with_tools
                from engines.shared.consultation_tools import (
                    get_personal_claude_md,
                    load_role_prompt,
                    get_role_footer,
                    ROLE_TRANSITION,
                )

                personal_claude = ""
                if has_github:
                    personal_claude = await get_personal_claude_md(user_chat_id)

                # DP.D.044: Role routing — detect if question needs Navigator or Diagnostician
                # WP-156: force_role applies to the FIRST question in /navigator session,
                # then clears. Subsequent questions use _detect_role() only.
                _session_force_role = session_ctx.get('force_role')
                if _session_force_role:
                    detected_role = _session_force_role
                    # Clear force_role after first use — subsequent questions route normally
                    del session_ctx['force_role']
                else:
                    detected_role = _detect_role(question) if not is_refinement else None
                role_prompt = None
                role_context_extra = None
                if detected_role:
                    role_prompt = load_role_prompt(detected_role)
                    if role_prompt:
                        # L2 Role Attribution: transition message
                        transition = ROLE_TRANSITION.get(detected_role, {})
                        transition_msg = transition.get(lang, transition.get("ru", ""))
                        if transition_msg:
                            await self.send(user, transition_msg, parse_mode="Markdown")
                        logger.info(f"Consultation: role switch → {detected_role} for user {user_chat_id}")

                        # WP-498 Ф5.1 (DP.M.386): mentor context-sufficiency gate,
                        # шаг 1 — детерминированный RAG-поиск PD.METHOD.* ДО
                        # генерации. Результат идёт ВНУТРЬ диспетчер-промпта
                        # (role_context_extra), не проверяется post-hoc.
                        if detected_role == "mentor":
                            from engines.shared.mentor_grounding import (
                                mentor_grounding_search,
                                format_grounding_section,
                            )
                            grounding = await mentor_grounding_search(question, user_chat_id)
                            role_context_extra = format_grounding_section(grounding, lang)

                # Conversation history → multi-turn messages
                history_messages = self._build_history_messages(session_ctx, question) if session_ctx.get('consultation_history') else None

                _t0 = time.time()
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
                    ui_tier=ui_tier,
                    role_prompt_override=role_prompt,
                    role_context_extra=role_context_extra,
                )
                logger.info("[Consultation] handle_question_with_tools done in %dms user=%s", int((time.time() - _t0) * 1000), user_chat_id)

                # L1 Role Attribution: footer with role signature
                if detected_role and role_prompt:
                    footer = get_role_footer(detected_role, lang)
                    if footer:
                        answer = answer.rstrip() + f"\n\n_{footer}_"

                logger.info(f"Consultation: T{tier}{' role=' + detected_role if detected_role else ''} for user {user_chat_id}")
                _answer_for_history = answer

                response = self._format_response(answer, sources, lang)

                typing_task.cancel()

                # Добавляем deep link если вопрос относится к сервису
                # (пропускаем если LLM уже упомянул команду в ответе)
                if response:
                    service_id = self._detect_service_intent(question)
                    if service_id:
                        service = registry.get(service_id)
                        if service and service.command and service.command not in response:
                            response += f"\n\n{service.icon} {t('consultation.try_service', lang)}: {service.command}"

                # Отправляем ответ с кнопками feedback
                chat_id = self._get_chat_id(user)
                qa_id = await get_latest_qa_id(chat_id) if chat_id else None

                reply_markup = None
                if qa_id:
                    reply_markup = _build_feedback_keyboard(qa_id, refinement_round, lang)

                parts = prepare_html_parts(response)
                for i, part in enumerate(parts):
                    is_last = (i == len(parts) - 1)
                    kb = reply_markup if is_last else None
                    await self.send(user, part, parse_mode="HTML", reply_markup=kb)

        except Exception as e:
            if typing_task:
                typing_task.cancel()
            logger.error(f"Consultation error: {e}", exc_info=True)
            await self.send(user, t('consultation.error', lang))
            return None

        # Сохраняем ответ в conversation history + записываем активный день
        try:
            if question and _answer_for_history:
                self._append_history(session_ctx, question, _answer_for_history)
                await self._save_session_context(chat_id, session_ctx)
                logger.info(f"[Consultation] History saved, {len(session_ctx.get('consultation_history', []))} pairs")
                # Записываем активный день (fire-and-forget)
                if chat_id:
                    from db.queries.activity import record_active_day
                    asyncio.create_task(record_active_day(chat_id, 'question_asked', mode=self._get_mode(user)))
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
                # Auto-triage (fire-and-forget)
                from core.feedback_triage import triage_feedback
                asyncio.create_task(triage_feedback(qa_comment_id, "comment"))
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
        timed_out = last_activity and (time.time() - last_activity) > SESSION_TIMEOUT_SEC

        # WP-156: Preserve force_role from session context for follow-up questions
        _session_role = ctx.get('force_role')

        # --- Вопрос с "?" → явный новый вопрос ---
        if text.startswith('?'):
            question = text[1:].strip()
            if question:
                if timed_out:
                    logger.info(f"[Consultation] Session timeout for chat {chat_id}, but new question received — restarting")
                    _saved_role = ctx.get('force_role')
                    self._clear_session(ctx)
                    if _saved_role:
                        ctx['force_role'] = _saved_role
                await self.enter(user, context={'question': question})
                return "followup"

        # --- Текст без "?" (≥3 символов) → follow-up вопрос ---
        if len(text) >= 3:
            if timed_out:
                logger.info(f"[Consultation] Session timeout for chat {chat_id}, but new question received — restarting")
                _saved_role = ctx.get('force_role')
                self._clear_session(ctx)
                if _saved_role:
                    ctx['force_role'] = _saved_role
            await self.enter(user, context={'question': text})
            return "followup"

        # Таймаут без содержательного текста → завершить
        if timed_out:
            logger.info(f"[Consultation] Session timeout for chat {chat_id}")
            await self._end_session(user, ctx, lang)
            return "done"

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
