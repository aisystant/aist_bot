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

import json
from typing import Optional, List, Tuple, Dict, Callable, Awaitable

from config import get_logger, ONTOLOGY_RULES
from core.intent import get_question_keywords
from clients import claude, mcp_knowledge
from db.queries.qa import save_qa, get_qa_history
from .retrieval import enhanced_search, get_retrieval
from .context import (
    build_dynamic_context,
    get_context_builder,
    DynamicContext,
)

logger = get_logger(__name__)


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

    logger.info(f"QuestionHandler: chat_id={chat_id}, mode={mode}")
    logger.info(f"QuestionHandler: исходный вопрос: '{question}'")
    logger.info(f"QuestionHandler: извлечённые ключевые слова: {keywords}")

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
        logger.info(f"QuestionHandler: итоговый поисковый запрос: '{search_query}'")
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
        logger.info(f"MCP-Knowledge: отправляю запрос '{query}'")
        results = await mcp_knowledge.search(query, limit=6)
        logger.info(f"MCP-Knowledge: получено {len(results) if results else 0} результатов")

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
            logger.warning(f"MCP-Knowledge: пустой результат для запроса '{query}'")
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
        'es': f"Pregunta: {question}"
    }
    user_prompt = user_prompts.get(lang, user_prompts['ru'])

    # Генерируем ответ
    answer = await claude.generate(system_prompt, user_prompt)

    if not answer:
        answer = f"К сожалению, {name}, не удалось получить ответ. Попробуйте переформулировать вопрос или спросить позже."

    return answer


async def handle_question_with_tools(
    question: str,
    intern: dict,
    context_topic: Optional[str] = None,
    bot_context: Optional[str] = None,
    has_digital_twin: bool = False,
    progress_callback: ProgressCallback = None,
) -> Tuple[str, List[str]]:
    """Обрабатывает вопрос через Claude tool_use (T2+ путь).

    Claude получает tools и САМ решает, когда искать в базе знаний
    или читать ЦД. Это заменяет ручной pre-search из handle_question().

    Args:
        question: текст вопроса
        intern: профиль пользователя
        context_topic: текущая тема
        bot_context: self-knowledge бота
        has_digital_twin: подключён ли ЦД (определяет набор tools)
        progress_callback: callback для отображения прогресса

    Returns:
        Tuple[answer, sources] - ответ и список источников
    """
    from .consultation_tools import (
        get_tools_for_tier,
        execute_tool,
        get_standard_claude_md,
    )
    from functools import partial

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

    # Собираем system prompt
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

    context_info = f"\nТекущая тема изучения: {context_topic}" if context_topic else ""
    occupation_info = f"\nПрофессия/занятие пользователя: {occupation}" if occupation else ""

    # Standard CLAUDE.md (T2+)
    standard_claude = get_standard_claude_md()
    standard_section = ""
    if standard_claude:
        standard_section = f"\n\nМЕТОДОЛОГИЯ:\n{standard_claude}"

    bot_section = ""
    if bot_context:
        bot_section = f"""

ЗНАНИЯ О БОТЕ:
{bot_context}
Если вопрос касается бота — отвечай ТОЛЬКО на основе информации выше."""

    system_prompt = f"""Ты — дружелюбный наставник по системному мышлению и личному развитию.
Отвечаешь на вопросы пользователя {name}.{occupation_info}{context_info}

{lang_instruction}

ПРАВИЛА:
1. Используй tools для поиска информации, когда нужен контекст для ответа.
2. НЕ выдумывай факты — если не нашёл в базе знаний, скажи об этом.
3. Отвечай кратко и по существу (3-5 абзацев максимум).
4. Используй простой язык, избегай академического стиля.
5. Если вопрос не по теме — вежливо перенаправь.
6. Если у пользователя есть Цифровой Двойник — используй read_digital_twin для персонализации.

{ONTOLOGY_RULES}
{standard_section}
{bot_section}

{lang_reminder}"""

    # Подготовка tools и executor
    tools = get_tools_for_tier(has_digital_twin)

    # Привязываем telegram_user_id к executor
    async def tool_executor(tool_name: str, tool_input: dict) -> str:
        return await execute_tool(tool_name, tool_input, telegram_user_id)

    await report_progress(ProcessingStage.ANALYZING, 20)

    # === ЭТАП 2-3: Claude с tools (20-95%) ===
    await report_progress(ProcessingStage.GENERATING, 30)

    # Формируем user message
    user_prompts = {
        'ru': f"Вопрос: {question}",
        'en': f"Question: {question}",
        'es': f"Pregunta: {question}"
    }
    user_prompt = user_prompts.get(lang, user_prompts['ru'])

    messages = [{"role": "user", "content": user_prompt}]

    answer = await claude.generate_with_tools(
        system_prompt=system_prompt,
        messages=messages,
        tools=tools,
        tool_executor=tool_executor,
        max_tokens=4000,
        max_tool_rounds=5,
    )

    await report_progress(ProcessingStage.DONE, 100)

    if not answer:
        answer = f"К сожалению, {name}, не удалось получить ответ. Попробуйте переформулировать вопрос или спросить позже."

    # Сохраняем в историю (sources пусто — Claude сам нашёл, не мы)
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
        'es': f"Pregunta: {question}"
    }
    user_prompt = user_prompts.get(lang, user_prompts['ru'])

    answer = await claude.generate(system_prompt, user_prompt)
    return answer or "Не удалось получить ответ. Попробуйте позже."
