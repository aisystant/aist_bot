"""
Обработчик вопросов пользователя.

Работает в любом режиме (Марафон/Лента).
Использует улучшенный Knowledge Retrieval (MCP) для поиска информации
и Claude для генерации ответа.

Поддерживает динамический контекст:
- Прогресс пользователя (день марафона, пройденные темы)
- История диалога (предыдущие вопросы в сессии)
- Метаданные темы (related_concepts, pain_point, key_insight)
"""

import asyncio
import hashlib
import json
from typing import Optional, List, Tuple, Dict, Callable, Awaitable

from config import get_logger, ONTOLOGY_RULES
from core.intent import get_question_keywords
from core.tracing import span
from clients import claude
from clients.gateway_mcp import gateway_mcp
from db.queries.qa import save_qa, get_qa_history
from db.queries.events import log_event
from .retrieval import enhanced_search, get_retrieval
from .context import (
    build_dynamic_context,
    get_context_builder,
    DynamicContext,
)

logger = get_logger(__name__)


def _hash_chat_id(chat_id) -> str:
    """Детерминированный 6-hex хеш chat_id для логов (PII-safe, cross-session)."""
    return hashlib.md5(str(chat_id).encode()).hexdigest()[:6]


# Маппинг complexity_level → стиль ответа
_COMPLEXITY_GUIDANCE = {
    1: {"ru": "Объясняй через простые аналогии и бытовые примеры. Избегай терминов.", "en": "Use simple analogies and everyday examples. Avoid jargon."},
    2: {"ru": "Используй базовую терминологию с пояснениями. Приводи практические примеры.", "en": "Use basic terminology with explanations. Give practical examples."},
    3: {"ru": "Предполагай знакомство с основами. Используй точную терминологию.", "en": "Assume basic knowledge. Use precise terminology."},
    4: {"ru": "Обсуждай на уровне практика: trade-offs, связи между концепциями, edge cases.", "en": "Discuss at practitioner level: trade-offs, concept connections, edge cases."},
    5: {"ru": "Глубокий экспертный уровень: архитектурные решения, мета-анализ, SOTA подходы.", "en": "Expert level: architectural decisions, meta-analysis, SOTA approaches."},
    6: {"ru": "Мастерский уровень: системное мышление второго порядка, создание фреймворков.", "en": "Mastery level: second-order systems thinking, framework creation."},
}


def _build_user_profile(intern: dict, lang: str) -> str:
    """Собрать секцию профиля пользователя для system prompt.

    Включает: интересы, цели, состояние, текущие проблемы, желания, роль.
    complexity_level НЕ показываем — это внутренний параметр марафона,
    не уровень мышления пользователя. Квалификации будут в ЦД.
    Возвращает пустую строку если данных нет.
    """
    parts = []

    # Интересы
    interests = intern.get('interests', [])
    if interests:
        if isinstance(interests, list):
            interests_str = ", ".join(interests[:5])
        else:
            interests_str = str(interests)[:200]
        if interests_str:
            parts.append(f"Интересы: {interests_str}")

    # Цели
    goals = intern.get('goals', '')
    if goals:
        parts.append(f"Цели: {goals[:200]}")

    # Роль
    role = intern.get('role', '')
    if role:
        parts.append(f"Роль: {role[:200]}")

    # Текущие проблемы
    current_problems = intern.get('current_problems', '')
    if current_problems:
        parts.append(f"Текущие проблемы: {current_problems[:300]}")

    # Желания
    desires = intern.get('desires', '')
    if desires:
        parts.append(f"Желания: {desires[:200]}")

    # Состояние (из теста)
    assessment = intern.get('assessment_state')
    if assessment:
        state_labels = {
            'chaos': 'Хаос (начало пути)',
            'deadlock': 'Тупик (нужен сдвиг)',
            'turning_point': 'Поворот (готов к изменениям)',
        }
        label = state_labels.get(assessment, assessment)
        parts.append(f"Состояние: {label}")

    if not parts:
        # Явно указываем, что данных нет — иначе Claude выдумывает профиль
        header = "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ" if lang != 'en' else "USER PROFILE"
        no_data = ("Профиль не заполнен. О пользователе ничего не известно. "
                   "НЕ выдумывай данные о пользователе (имя, роль, интересы). "
                   "Если спрашивают — скажи, что профиль пуст, и предложи заполнить через /profile."
                   ) if lang != 'en' else (
                   "Profile is empty. Nothing is known about the user. "
                   "Do NOT fabricate user data. Suggest filling profile via /profile.")
        return f"\n{header}:\n- {no_data}"

    header = "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ" if lang != 'en' else "USER PROFILE"
    return f"\n{header}:\n" + "\n".join(f"- {p}" for p in parts)


# Типы для progress callback
ProgressCallback = Callable[[str, int], Awaitable[None]]
"""Callback для отображения прогресса: (stage_name, percent) -> None"""


# Этапы обработки
class ProcessingStage:
    """Константы этапов обработки для progress callback"""
    ANALYZING = "analyzing"        # Анализ вопроса
    SEARCHING = "searching"        # Поиск в базе знаний
    GENERATING = "generating"      # Генерация ответа
    DONE = "done"                  # Завершено


async def handle_question(
    question: str,
    intern: dict,
    context_topic: Optional[str] = None,
    topic_id: Optional[str] = None,
    knowledge_structure: dict = None,
    use_enhanced_retrieval: bool = True,
    progress_callback: ProgressCallback = None,
    bot_context: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Обрабатывает вопрос пользователя и генерирует ответ

    Args:
        question: текст вопроса
        intern: профиль пользователя
        context_topic: текущая тема (для контекста) - название темы
        topic_id: ID темы (для загрузки метаданных)
        knowledge_structure: структура знаний (для метаданных темы)
        use_enhanced_retrieval: использовать улучшенный retrieval (по умолчанию True)
        progress_callback: callback для отображения прогресса (stage, percent)
        bot_context: self-knowledge бота для system prompt (из core.self_knowledge)

    Returns:
        Tuple[answer, sources] - ответ и список источников
    """
    chat_id = intern.get('chat_id')
    mode = intern.get('mode', 'marathon')

    # Helper для вызова progress callback
    async def report_progress(stage: str, percent: int):
        if progress_callback:
            try:
                await progress_callback(stage, percent)
            except Exception as e:
                logger.debug(f"Progress callback error: {e}")

    # === ЭТАП 1: Анализ вопроса (0-20%) ===
    await report_progress(ProcessingStage.ANALYZING, 10)

    # Извлекаем ключевые слова для поиска
    keywords = get_question_keywords(question)
    search_query = ' '.join(keywords) if keywords else question[:100]

    # Если ключевые слова сильно отличаются от исходного вопроса,
    # используем исходный вопрос — MCP semantic search лучше работает с natural language
    if len(keywords) <= 1 or (len(keywords) <= 2 and len(question) > 30):
        search_query = question[:150]

    logger.info(f"QuestionHandler: user_hash={_hash_chat_id(chat_id)}, mode={mode}")
    logger.info(f"QuestionHandler: вопрос len={len(question)}")
    logger.info(f"QuestionHandler: извлечённые ключевые слова count={len(keywords)}")

    if context_topic:
        logger.info(f"QuestionHandler: контекст темы: '{context_topic}'")

    # Строим динамический контекст
    dynamic_context = None
    if use_enhanced_retrieval:
        try:
            dynamic_context = await build_dynamic_context(
                intern=intern,
                topic_id=topic_id,
                qa_history_loader=get_qa_history,
                knowledge_structure=knowledge_structure
            )
            logger.info(f"QuestionHandler: динамический контекст построен, "
                       f"boost_concepts={len(dynamic_context.boost_concepts)}")
        except Exception as e:
            logger.warning(f"QuestionHandler: ошибка построения контекста: {e}")

    await report_progress(ProcessingStage.ANALYZING, 20)

    # === ЭТАП 2: Поиск в базе знаний (20-60%) ===
    await report_progress(ProcessingStage.SEARCHING, 30)

    # Ищем информацию через MCP (улучшенный или базовый retrieval)
    if use_enhanced_retrieval:
        logger.info("QuestionHandler: используем EnhancedRetrieval")
        mcp_context, sources = await enhanced_search(
            query=search_query,
            keywords=keywords,
            context_topic=context_topic,
            dynamic_context=dynamic_context
        )
    else:
        # Fallback на старый метод
        if context_topic:
            search_query = f"{context_topic} {search_query}"
        logger.info(f"QuestionHandler: итоговый поисковый запрос len={len(search_query)}")
        mcp_context, sources = await search_mcp_context(search_query)

    await report_progress(ProcessingStage.SEARCHING, 60)

    # === ЭТАП 3: Генерация ответа (60-95%) ===
    await report_progress(ProcessingStage.GENERATING, 70)
    answer = await generate_answer(
        question, intern, mcp_context, context_topic, dynamic_context,
        bot_context=bot_context,
    )

    await report_progress(ProcessingStage.DONE, 100)

    # Сохраняем в историю
    if chat_id:
        try:
            await save_qa(
                chat_id=chat_id,
                mode=mode,
                context_topic=context_topic or '',
                question=question,
                answer=answer,
                mcp_sources=sources
            )
            # ЦД: событие ai_chat (WP-85, WP-151 Ф3: расширенный payload)
            await log_event(chat_id, 'ai_chat', {
                'mode': mode,
                'question_length': len(question),
                'answer_length': len(answer) if answer else 0,
                'context_topic': context_topic or None,
                'has_sources': bool(sources),
                'source_count': len(sources) if sources else 0,
            })
        except Exception as e:
            logger.error(f"Ошибка сохранения Q&A: {e}")

    return answer, sources


async def search_mcp_context(query: str) -> Tuple[str, List[str]]:
    """Ищет релевантную информацию через MCP серверы (DEPRECATED)

    DEPRECATED: Используйте enhanced_search() из retrieval.py для улучшенного поиска
    с query expansion, relevance scoring и семантической дедупликацией.

    Args:
        query: поисковый запрос

    Returns:
        Tuple[context, sources] - контекст и список источников
    """
    context_parts = []
    sources = []
    seen_texts = set()

    # Поиск в unified Knowledge MCP (все источники: pack + guides + ds)
    try:
        logger.info(f"Gateway-Knowledge: отправляю запрос, len={len(query)}")
        # WP-330: span на pre-search — отделяет его время от tool-раундов Claude
        # и ловит дубль (если Claude повторно зовёт knowledge_search через tool).
        async with span("consultation.presearch", query_len=len(query)):
            results = await gateway_mcp.knowledge_search(query, limit=6)
        logger.info(f"Gateway-Knowledge: получено {len(results) if results else 0} результатов")

        if results:
            first_item = results[0]
            if isinstance(first_item, dict):
                logger.debug(f"MCP-Knowledge первый результат (ключи): {list(first_item.keys())}")

            for item in results:
                text = extract_text(item)
                if text and text[:100] not in seen_texts:
                    seen_texts.add(text[:100])
                    if isinstance(item, dict):
                        source = item.get('source', item.get('title', ''))
                        source_type = item.get('source_type', 'pack')
                        if source_type == 'guides':
                            if source and f"Руководство: {source}" not in sources:
                                sources.append(f"Руководство: {source}")
                        else:
                            if source and f"База знаний: {source}" not in sources:
                                sources.append(f"База знаний: {source}")
                    context_parts.append(text[:1500])
        else:
            logger.warning(f"MCP-Knowledge: пустой результат, запрос len={len(query)}")
    except Exception as e:
        logger.error(f"MCP-Knowledge search error: {e}", exc_info=True)

    # Объединяем контекст
    if context_parts:
        context = "\n\n---\n\n".join(context_parts[:5])
        logger.info(f"MCP итого: {len(context_parts)} фрагментов, {len(context)} символов контекста")
        logger.info(f"MCP источники: {sources}")
    else:
        context = ""
        logger.warning(f"MCP итого: контекст пустой — оба MCP не вернули результатов")

    return context, sources


def extract_text(item) -> str:
    """Извлекает текст из результата поиска MCP

    Args:
        item: результат из MCP (dict или str)

    Returns:
        Текст содержимого
    """
    if isinstance(item, dict):
        return item.get('text', item.get('content', item.get('snippet', '')))
    elif isinstance(item, str):
        return item
    return ''


async def generate_answer(
    question: str,
    intern: dict,
    mcp_context: str,
    context_topic: Optional[str] = None,
    dynamic_context: DynamicContext = None,
    bot_context: Optional[str] = None,
) -> str:
    """Генерирует ответ на вопрос через Claude

    Args:
        question: вопрос пользователя
        intern: профиль пользователя
        mcp_context: контекст из MCP
        context_topic: текущая тема для контекста
        dynamic_context: динамический контекст (прогресс, история, метаданные)
        bot_context: self-knowledge бота (из core.self_knowledge)

    Returns:
        Текст ответа
    """
    name = intern.get('name', 'пользователь')
    occupation = intern.get('occupation', '')
    complexity = intern.get('complexity_level', intern.get('bloom_level', 1))
    lang = intern.get('language', 'ru')

    # Определяем язык ответа
    lang_instruction = {
        'ru': "ВАЖНО: Отвечай на русском языке.",
        'en': "IMPORTANT: Answer in English.",
        'es': "IMPORTANTE: Responde en español.",
        'fr': "IMPORTANT: Réponds en français."
    }.get(lang, "IMPORTANT: Answer in English.")

    lang_reminder = {
        'ru': "НАПОМИНАНИЕ: Весь ответ должен быть на РУССКОМ языке!",
        'en': "REMINDER: The entire answer must be in ENGLISH!",
        'es': "RECORDATORIO: ¡Toda la respuesta debe estar en ESPAÑOL!",
        'fr': "RAPPEL: Toute la réponse doit être en FRANÇAIS!"
    }.get(lang, "REMINDER: The entire answer must be in ENGLISH!")

    # Формируем системный промпт
    context_info = ""
    if context_topic:
        context_info = f"\nТекущая тема изучения: {context_topic}"

    occupation_info = ""
    if occupation:
        occupation_info = f"\nПрофессия/занятие пользователя: {occupation}"

    # Профиль пользователя: уровень + интересы + цели
    user_profile_info = _build_user_profile(intern, lang)

    # Добавляем дополнения из динамического контекста
    dynamic_sections = ""
    if dynamic_context:
        builder = get_context_builder()
        additions = builder.get_prompt_additions(dynamic_context)

        if additions.get('progress_summary'):
            dynamic_sections += f"\n{additions['progress_summary']}"

        if additions.get('topic_context'):
            dynamic_sections += f"\n\n{additions['topic_context']}"

        if additions.get('conversation_history'):
            dynamic_sections += f"\n\n{additions['conversation_history']}"

    mcp_section = ""
    if mcp_context:
        mcp_section = f"""

ИНФОРМАЦИЯ ИЗ МАТЕРИАЛОВ AISYSTANT:
{mcp_context}

Используй эту информацию для ответа, но адаптируй под вопрос пользователя."""

    # Инструкция по источникам убрана — провоцировала генерацию вне контекста (P1 fix)
    sources_instruction = ""

    bot_section = ""
    if bot_context:
        bot_section = f"""

ЗНАНИЯ О БОТЕ:
{bot_context}
Если вопрос касается бота — отвечай ТОЛЬКО на основе информации выше. НЕ приписывай боту функции, которых нет в списке сценариев. Если функция не указана — скажи, что такой возможности пока нет."""

    system_prompt = f"""Ты — дружелюбный наставник по системному мышлению и личному развитию.
Отвечаешь на вопросы пользователя {name}.{occupation_info}{context_info}{dynamic_sections}
{user_profile_info}
{lang_instruction}

ПРАВИЛА (в порядке приоритета):
1. ГРАНИЦА ЗНАНИЙ (высший приоритет): Отвечай ТОЛЬКО на основе ИНФОРМАЦИИ ИЗ МАТЕРИАЛОВ и ДАННЫХ МАРАФОНА ниже. НЕ выдумывай факты, примеры, названия тем или номера дней, которых нет в контексте. Лучше короткий точный ответ, чем длинный с домыслами.
2. Если в контексте нет ответа — скажи: «В доступных мне материалах этого нет. Попробуйте спросить иначе.»
3. Отвечай кратко и по существу (3-5 абзацев максимум)
4. Используй простой язык, избегай академического стиля
5. Если вопрос не по теме системного мышления — вежливо перенаправь

{ONTOLOGY_RULES}
{mcp_section}
{bot_section}

{lang_reminder}"""

    # Локализуем промпт
    user_prompts = {
        'ru': f"Вопрос: {question}",
        'en': f"Question: {question}",
        'es': f"Pregunta: {question}",
        'fr': f"Question : {question}",
        'zh': f"问题：{question}"
    }
    user_prompt = user_prompts.get(lang, user_prompts['ru'])

    # Генерируем ответ
    answer = await claude.generate(system_prompt, user_prompt)

    if not answer:
        display_name = name if name else ""
        answer = (
            f"{display_name + ', ' if display_name else ''}"
            "внешний сервис временно недоступен. "
            "Попробуйте через минуту — обычно это быстро проходит."
        )

    return answer


async def handle_question_with_tools(
    question: str,
    intern: dict,
    context_topic: Optional[str] = None,
    bot_context: Optional[str] = None,
    has_digital_twin: bool = False,
    personal_claude_md: Optional[str] = None,
    progress_callback: ProgressCallback = None,
    tier: int = 1,
    is_refinement: bool = False,
    conversation_messages: Optional[List[Dict]] = None,
    ui_tier: int = -1,
    role_prompt_override: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Обрабатывает вопрос через Claude tool_use (все тиры T1-T4).

    Claude получает tools и САМ решает, когда искать в базе знаний
    или читать ЦД. System prompt загружается из config/prompts/t{N}_{role}.md.

    Тиры (DP.ARCH.002):
    - T1 Expert: search_knowledge + search_guides
    - T2 Mentor: + read_digital_twin + standard CLAUDE.md
    - T3 Co-thinker: + personal CLAUDE.md
    - T4 Architect: = T3 (future: + read_file, git_log)

    Args:
        question: текст вопроса
        intern: профиль пользователя
        context_topic: текущая тема
        bot_context: self-knowledge бота
        has_digital_twin: подключён ли ЦД (определяет набор tools)
        personal_claude_md: персональный CLAUDE.md из GitHub (T3)
        progress_callback: callback для отображения прогресса
        tier: тир обслуживания (1-4, default 1)
        conversation_messages: multi-turn conversation history
            (list of {role, content} dicts from persistent session)

    Returns:
        Tuple[answer, sources] - ответ и список источников
    """
    from .consultation_tools import (
        get_tools_for_tier,
        execute_tool,
        load_tier_prompt,
        fill_tier_prompt,
        get_wp_registry,
    )
    from .wp_query_detector import is_wp_query

    chat_id = intern.get('chat_id')
    mode = intern.get('mode', 'marathon')
    lang = intern.get('language', 'ru')
    telegram_user_id = intern.get('chat_id')  # chat_id = telegram_user_id

    async def report_progress(stage: str, percent: int):
        if progress_callback:
            try:
                await progress_callback(stage, percent)
            except Exception as e:
                logger.debug(f"Progress callback error: {e}")

    # === ЭТАП 1: Подготовка (0-20%) ===
    await report_progress(ProcessingStage.ANALYZING, 10)

    name = intern.get('name', 'пользователь')
    occupation = intern.get('occupation', '')

    lang_instruction = {
        'ru': "ВАЖНО: Отвечай на русском языке.",
        'en': "IMPORTANT: Answer in English.",
        'es': "IMPORTANTE: Responde en español.",
        'fr': "IMPORTANT: Réponds en français."
    }.get(lang, "IMPORTANT: Answer in English.")

    lang_reminder = {
        'ru': "НАПОМИНАНИЕ: Весь ответ должен быть на РУССКОМ языке!",
        'en': "REMINDER: The entire answer must be in ENGLISH!",
        'es': "RECORDATORIO: ¡Toda la respuesta debe estar en ESPAÑOL!",
        'fr': "RAPPEL: Toute la réponse doit être en FRANÇAIS!"
    }.get(lang, "REMINDER: The entire answer must be in ENGLISH!")

    # Текущая дата в Москве — Claude иначе угадывает её по WeekPlan-фразам и
    # ошибается на 1-2 дня (тестирование WP-209 Ф3, замечание #3, 11 апр 2026)
    from datetime import datetime, timezone, timedelta
    _msk_now = datetime.now(timezone.utc) + timedelta(hours=3)
    _date_info = f"\nСегодняшняя дата (МСК): {_msk_now.strftime('%Y-%m-%d')} ({['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'][_msk_now.weekday()]})"

    context_info = (_date_info + (f"\nТекущая тема изучения: {context_topic}" if context_topic else ""))
    occupation_info = f"\nПрофессия/занятие пользователя: {occupation}" if occupation else ""

    # Context Pipeline: collectors по тиру (параллельно)
    # Pre-search включён: knowledge-mcp вызывается ДО Claude,
    # результаты в {knowledge_section}. Claude видит релевантные документы
    # даже если не вызовет search_knowledge tool.
    from .context_pipeline import assemble_context
    async with span("consultation.assemble_context", tier=tier):
        sections = await assemble_context(
            tier=tier,
            intern=intern,
            lang=lang,
            bot_context=bot_context or "",
            personal_claude_md=personal_claude_md or "",
            ui_tier=ui_tier,
            question=question,
        )

    # Загружаем шаблон промпта и подставляем переменные
    # DP.D.044: role_prompt_override заменяет tier-промпт при смене роли
    template = role_prompt_override if role_prompt_override else load_tier_prompt(tier)
    # format_rules: канальная адаптация (DP.D.044). Бот = Telegram rules.
    _TG_FORMAT_RULES = (
        "ФОРМАТИРОВАНИЕ (Telegram):\n"
        "- ЗАПРЕЩЕНО использовать таблицы (| ... | ... |). Telegram их НЕ отображает.\n"
        "- ЗАПРЕЩЕНО использовать markdown-заголовки (# ## ###). Вместо них — *жирный текст*.\n"
        "- Списки (• или -) с *жирным* для заголовков.\n"
        "- Перечисления оформляй списком (каждый пункт на отдельной строке), НИКОГДА не через запятую в одном абзаце.\n"
        "- Короткие абзацы (2-3 предложения).\n"
        "- Команды бота пиши как обычный текст (не в обратных кавычках)."
    )
    system_prompt = fill_tier_prompt(
        template,
        name=name,
        occupation_info=occupation_info,
        context_info=context_info,
        lang_instruction=lang_instruction,
        lang_reminder=lang_reminder,
        ontology_rules=ONTOLOGY_RULES,
        format_rules=_TG_FORMAT_RULES,
        **sections,
    )

    logger.info(f"Consultation T{tier}: prompt {len(system_prompt)} chars for user_hash={_hash_chat_id(telegram_user_id)}")

    # Подготовка tools и executor
    tools = get_tools_for_tier(has_digital_twin)

    # WP-411 Ф7: детерминированная инжекция реестра РП на вопрос «мои РП».
    # Без неё бот сочинял несуществующие РП (контекст-пайплайн реестр не тянет,
    # personal_search его не находит — реестр помечен index-health:skip).
    # Вариант В: fetch docs/WP-REGISTRY.md по пути из strategy_repo пользователя.
    if is_wp_query(question):
        # get_wp_registry сам возвращает "" если у пользователя нет strategy_repo —
        # гейтить по has_digital_twin нельзя: реестр живёт в GitHub (strategy_repo),
        # это ось, независимая от ЦД (T3 = github без ЦД тоже имеет реестр).
        registry_text = ""
        try:
            registry_text = await asyncio.wait_for(
                get_wp_registry(telegram_user_id), timeout=5
            )
        except Exception:
            logger.warning(
                f"WP-411 Ф7: registry fetch failed/timeout for "
                f"user_hash={_hash_chat_id(telegram_user_id)}",
                exc_info=True,
            )
        if registry_text:
            system_prompt += (
                "\n\nРЕЕСТР РАБОЧИХ ПРОДУКТОВ ПОЛЬЗОВАТЕЛЯ (источник истины, актуальный):\n"
                + registry_text
                + "\n\nНа вопрос про РП / реестр / рабочие продукты пользователя отвечай "
                "ТОЛЬКО из этого списка. Не выдумывай и не добавляй РП вне списка."
            )
            logger.info(
                f"WP-411 Ф7: injected registry ({len(registry_text)} chars) "
                f"for user_hash={_hash_chat_id(telegram_user_id)}"
            )
        else:
            system_prompt += (
                "\n\n<no_wp_data/>\n"
                "У тебя НЕТ доступа к реестру рабочих продуктов (РП) этого пользователя. "
                "На вопрос про «мои РП / мой реестр / мои задачи / мои рабочие продукты» "
                "отвечай ТОЛЬКО: «У меня нет доступа к вашему реестру РП.» "
                "ЗАПРЕЩЕНО: выдумывать РП, угадывать или перечислять несуществующие "
                "названия и проекты."
            )
            logger.info(
                f"WP-411 Ф7: no registry data → <no_wp_data/> "
                f"for user_hash={_hash_chat_id(telegram_user_id)}"
            )

    # WP-330 (peer-session 2026-06-05-34): таймаут на отдельный tool call.
    # Без него max_tool_rounds×35с (gateway timeout) давали потолок до 105с.
    # 18с выше медианы knowledge_search (2-6с, в пике 12-15с), но режет worst-case.
    PER_TOOL_TIMEOUT = 18

    # cold-review (subagent, 2026-06-05, итерация 2): wait_for добавляет точку отмены.
    # Discovered-инструменты шлюза имеют доменный префикс ПЕРВЫМ (knowledge_search,
    # dt_read_*, personal_*) — prefix-match их пропускал, и основной knowledge_search
    # оставался без таймаута. Новая логика: cancel-safe = есть read-маркер где угодно
    # в имени И нет ни одного мутирующего маркера. Инвариант (главное): ни один
    # write-инструмент (dt_write, personal_write, grant_*, *reindex*, create_*) НЕ
    # получает wait_for — мутирующая проверка идёт первой и возвращает False.
    _MUTATING_MARKERS = ("write", "grant", "revoke", "connect", "disconnect",
                         "create", "delete", "update", "purge", "reindex",
                         "scaffold", "propose", "redeem", "upsert", "set_",
                         "run_", "request_", "load_skill", "feedback", "chat",
                         "send", "extractor", "strategist", "remind")
    _READONLY_MARKERS = ("search", "get", "read", "list", "describe", "analyze",
                         "expand", "status", "traverse", "concept", "graph",
                         "learner", "document", "brief", "stats", "progress")

    def _is_cancel_safe(name: str) -> bool:
        n = name.lower()
        if any(m in n for m in _MUTATING_MARKERS):
            return False  # никогда не отменять потенциальную запись
        return any(r in n for r in _READONLY_MARKERS)

    async def tool_executor(tool_name: str, tool_input: dict) -> str:
        async with span(f"tool.{tool_name}"):
            if not _is_cancel_safe(tool_name):
                # без wait_for: нельзя отменять потенциально мутирующую операцию
                return await execute_tool(tool_name, tool_input, telegram_user_id)
            try:
                return await asyncio.wait_for(
                    execute_tool(tool_name, tool_input, telegram_user_id),
                    timeout=PER_TOOL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[Consultation] tool '{tool_name}' превысил {PER_TOOL_TIMEOUT}с — "
                    f"возвращаю пустой результат, Claude продолжит цикл"
                )
                return (
                    f"Инструмент {tool_name} не ответил за {PER_TOOL_TIMEOUT}с. "
                    f"Ответь на основе уже доступного контекста, не вызывай этот инструмент повторно."
                )

    await report_progress(ProcessingStage.ANALYZING, 20)

    # === ЭТАП 2-3: Claude с tools (20-95%) ===
    await report_progress(ProcessingStage.GENERATING, 30)

    user_prompts = {
        'ru': f"Вопрос: {question}",
        'en': f"Question: {question}",
        'es': f"Pregunta: {question}",
        'fr': f"Question : {question}",
        'zh': f"问题：{question}"
    }
    user_prompt = user_prompts.get(lang, user_prompts['ru'])

    # WP-330 P3: pre-search + synthetic tool-result injection.
    # Устраняет дублирующий вызов search_knowledge в tool-раундах Claude (−14с).
    # Claude видит tool_result с результатами → не повторяет поиск.
    # Fallback: если шлюз вернул пусто → идём без инжекта (Claude сам позовёт).
    pre_results = ""
    try:
        async with span("consultation.presearch_p3", query_len=len(question)):
            raw_presearch = await gateway_mcp.knowledge_search(question, limit=6)
        if raw_presearch:
            fragments = []
            seen = set()
            for item in raw_presearch:
                text = extract_text(item) if isinstance(item, dict) else (item or "")
                if text and text[:100] not in seen:
                    seen.add(text[:100])
                    fragments.append(text[:1500])
            pre_results = "\n\n---\n\n".join(fragments[:5])
        logger.info(f"P3 presearch: {len(fragments) if raw_presearch else 0} фрагментов")
    except Exception as e:
        logger.warning(f"P3 presearch failed, fallback to tool-only: {e}")

    # Строим messages с synthetic tool-result (если pre-search дал результаты)
    base_messages = conversation_messages if conversation_messages else [
        {"role": "user", "content": user_prompt}
    ]
    if pre_results:
        synthetic_id = "presearch_0"
        inject = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": synthetic_id,
                "name": "search_knowledge", "input": {"query": question}}]},
            {"role": "user", "content": [{"type": "tool_result",
                "tool_use_id": synthetic_id, "content": pre_results}]},
        ]
        messages = base_messages + inject
        logger.info(f"P3: synthetic tool-result injected ({len(pre_results)} chars)")
    else:
        messages = base_messages
        logger.info(f"Consultation: {'multi-turn' if conversation_messages else 'new'} "
                    f"with {len(messages)} messages (no presearch inject)")

    # WP-7 fix (W14): 2500 недостаточно для русского + tool_use overhead
    # (5 feedback K-category за ночь 2026-04-02). Tool rounds съедают ~700-1000 tok,
    # на ответ оставалось ~1500 → обрезка. Унифицировано до 4000.
    token_limit = 4000

    # WP-330: max_tool_rounds 3→2. Pre-search knowledge_search уже выполнен (P3 inject),
    # большинство консультаций укладываются в 0-1 раунд. Force-text fallback
    # (clients/claude.py §9) гарантирует ответ при исчерпании раундов.
    async with span("consultation.claude_with_tools", max_tool_rounds=2):
        answer = await claude.generate_with_tools(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            tool_executor=tool_executor,
            max_tokens=token_limit,
            max_tool_rounds=2,
        )

    await report_progress(ProcessingStage.DONE, 100)

    if not answer:
        # WP-209: диагностика причины None от Claude API (PII-safe)
        logger.error(
            f"generate_with_tools returned None for user_hash={_hash_chat_id(chat_id)}, "
            f"question_len={len(question)}, tools={[t['name'] for t in tools]}, "
            f"system_prompt_len={len(system_prompt)}, token_limit={token_limit}"
        )
        display_name = name if name else ""
        answer = (
            f"{display_name + ', ' if display_name else ''}"
            "внешний сервис временно недоступен. "
            "Попробуйте через минуту — обычно это быстро проходит."
        )

    # Сохраняем в историю
    sources: List[str] = []
    if chat_id:
        try:
            await save_qa(
                chat_id=chat_id,
                mode=mode,
                context_topic=context_topic or '',
                question=question,
                answer=answer,
                mcp_sources=sources
            )
            # ЦД: событие ai_chat (WP-85, WP-151 Ф3: расширенный payload)
            await log_event(chat_id, 'ai_chat', {
                'mode': mode,
                'question_length': len(question),
                'answer_length': len(answer) if answer else 0,
                'context_topic': context_topic or None,
                'has_sources': bool(sources),
                'source_count': len(sources) if sources else 0,
                'has_tool_use': True,
            })
        except Exception as e:
            logger.error(f"Ошибка сохранения Q&A: {e}")

    return answer, sources


async def answer_with_context(
    question: str,
    intern: dict,
    additional_context: str = ""
) -> str:
    """Упрощённый метод для ответа с дополнительным контекстом

    Используется когда контекст уже известен (например, из текущей темы).

    Args:
        question: вопрос пользователя
        intern: профиль пользователя
        additional_context: дополнительный контекст

    Returns:
        Текст ответа
    """
    name = intern.get('name', 'пользователь')
    occupation = intern.get('occupation', '')

    # Определяем язык пользователя
    lang = intern.get('language', 'ru')
    lang_instruction = {
        'ru': "ВАЖНО: Отвечай на русском языке.",
        'en': "IMPORTANT: Answer in English.",
        'es': "IMPORTANTE: Responde en español.",
        'fr': "IMPORTANT: Réponds en français."
    }.get(lang, "IMPORTANT: Answer in English.")

    lang_reminder = {
        'ru': "НАПОМИНАНИЕ: Весь ответ должен быть на РУССКОМ языке!",
        'en': "REMINDER: The entire answer must be in ENGLISH!",
        'es': "RECORDATORIO: ¡Toda la respuesta debe estar en ESPAÑOL!",
        'fr': "RAPPEL: Toute la réponse doit être en FRANÇAIS!"
    }.get(lang, "REMINDER: The entire answer must be in ENGLISH!")

    occupation_info = f"\nПрофессия: {occupation}" if occupation else ""
    context_section = f"\n\nКОНТЕКСТ:\n{additional_context}" if additional_context else ""

    system_prompt = f"""Ты — дружелюбный наставник по системному мышлению.
Отвечаешь на вопрос пользователя {name}.{occupation_info}
{lang_instruction}

Отвечай кратко и по существу.

{ONTOLOGY_RULES}
{context_section}

{lang_reminder}"""

    # Локализуем промпт
    user_prompts = {
        'ru': f"Вопрос: {question}",
        'en': f"Question: {question}",
        'es': f"Pregunta: {question}",
        'fr': f"Question : {question}",
        'zh': f"问题：{question}"
    }
    user_prompt = user_prompts.get(lang, user_prompts['ru'])

    answer = await claude.generate(system_prompt, user_prompt)
    return answer or "Не удалось получить ответ. Попробуйте позже."
