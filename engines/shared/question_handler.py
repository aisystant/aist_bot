"""
Обработчик вопросов пользователя.

Работает в любом режиме (Марафон/Лента).
Использует улучшенный Knowledge Retrieval (MCP) для поиска информации
и Claude для генерации ответа.
"""

import json
from typing import Optional, List, Tuple

from config import get_logger
from core.intent import get_question_keywords
from clients import claude, mcp_guides, mcp_knowledge
from db.queries.qa import save_qa
from .retrieval import enhanced_search, get_retrieval

logger = get_logger(__name__)


async def handle_question(
    question: str,
    intern: dict,
    context_topic: Optional[str] = None,
    use_enhanced_retrieval: bool = True,
) -> Tuple[str, List[str]]:
    """Обрабатывает вопрос пользователя и генерирует ответ

    Args:
        question: текст вопроса
        intern: профиль пользователя
        context_topic: текущая тема (для контекста)
        use_enhanced_retrieval: использовать улучшенный retrieval (по умолчанию True)

    Returns:
        Tuple[answer, sources] - ответ и список источников
    """
    chat_id = intern.get('chat_id')
    mode = intern.get('mode', 'marathon')

    # Извлекаем ключевые слова для поиска
    keywords = get_question_keywords(question)
    search_query = ' '.join(keywords) if keywords else question[:100]

    logger.info(f"QuestionHandler: chat_id={chat_id}, mode={mode}")
    logger.info(f"QuestionHandler: исходный вопрос: '{question}'")
    logger.info(f"QuestionHandler: извлечённые ключевые слова: {keywords}")

    if context_topic:
        logger.info(f"QuestionHandler: контекст темы: '{context_topic}'")

    # Ищем информацию через MCP (улучшенный или базовый retrieval)
    if use_enhanced_retrieval:
        logger.info("QuestionHandler: используем EnhancedRetrieval")
        mcp_context, sources = await enhanced_search(
            query=search_query,
            keywords=keywords,
            context_topic=context_topic
        )
    else:
        # Fallback на старый метод
        if context_topic:
            search_query = f"{context_topic} {search_query}"
        logger.info(f"QuestionHandler: итоговый поисковый запрос: '{search_query}'")
        mcp_context, sources = await search_mcp_context(search_query)

    # Генерируем ответ через Claude
    answer = await generate_answer(question, intern, mcp_context, context_topic)

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

    # Поиск в руководствах (MCP-Guides)
    try:
        logger.info(f"MCP-Guides: отправляю запрос '{query}'")
        guides_results = await mcp_guides.semantic_search(query, lang="ru", limit=3)
        logger.info(f"MCP-Guides: получено {len(guides_results) if guides_results else 0} результатов")

        if guides_results:
            # Логируем первый результат для отладки
            first_item = guides_results[0]
            if isinstance(first_item, dict):
                logger.debug(f"MCP-Guides первый результат (ключи): {list(first_item.keys())}")
                preview = extract_text(first_item)[:200]
                logger.debug(f"MCP-Guides первый результат (превью): {preview}...")
            else:
                logger.debug(f"MCP-Guides первый результат (тип): {type(first_item)}")

            for item in guides_results:
                text = extract_text(item)
                if text and text[:100] not in seen_texts:
                    seen_texts.add(text[:100])
                    context_parts.append(text[:1500])
                    # Добавляем источник если есть
                    if isinstance(item, dict):
                        source = item.get('source', item.get('guide', ''))
                        if source and source not in sources:
                            sources.append(f"Руководство: {source}")
        else:
            logger.warning(f"MCP-Guides: пустой результат для запроса '{query}'")
    except Exception as e:
        logger.error(f"MCP-Guides search error: {e}", exc_info=True)

    # Поиск в базе знаний (MCP-Knowledge)
    try:
        logger.info(f"MCP-Knowledge: отправляю запрос '{query}'")
        knowledge_results = await mcp_knowledge.search(query, limit=3)
        logger.info(f"MCP-Knowledge: получено {len(knowledge_results) if knowledge_results else 0} результатов")

        if knowledge_results:
            # Логируем первый результат для отладки
            first_item = knowledge_results[0]
            if isinstance(first_item, dict):
                logger.debug(f"MCP-Knowledge первый результат (ключи): {list(first_item.keys())}")
                preview = extract_text(first_item)[:200]
                logger.debug(f"MCP-Knowledge первый результат (превью): {preview}...")
            else:
                logger.debug(f"MCP-Knowledge первый результат (тип): {type(first_item)}")

            for item in knowledge_results:
                text = extract_text(item)
                if text and text[:100] not in seen_texts:
                    seen_texts.add(text[:100])
                    # Добавляем дату если есть
                    if isinstance(item, dict):
                        date_info = item.get('created_at', item.get('date', ''))
                        if date_info:
                            text = f"[{date_info}] {text}"
                        source = item.get('source', item.get('title', ''))
                        if source and source not in sources:
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
    context_topic: Optional[str] = None
) -> str:
    """Генерирует ответ на вопрос через Claude

    Args:
        question: вопрос пользователя
        intern: профиль пользователя
        mcp_context: контекст из MCP
        context_topic: текущая тема для контекста

    Returns:
        Текст ответа
    """
    name = intern.get('name', 'пользователь')
    occupation = intern.get('occupation', '')
    complexity = intern.get('complexity_level', intern.get('bloom_level', 1))

    # Формируем системный промпт
    context_info = ""
    if context_topic:
        context_info = f"\nТекущая тема изучения: {context_topic}"

    occupation_info = ""
    if occupation:
        occupation_info = f"\nПрофессия/занятие пользователя: {occupation}"

    mcp_section = ""
    if mcp_context:
        mcp_section = f"""

ИНФОРМАЦИЯ ИЗ МАТЕРИАЛОВ AISYSTANT:
{mcp_context}

Используй эту информацию для ответа, но адаптируй под вопрос пользователя."""

    # Инструкция по научным источникам в зависимости от сложности
    max_sources = min(complexity, 3)  # 1, 2 или 3 источника
    sources_instruction = f"""
6. НАУЧНЫЕ ИСТОЧНИКИ (опционально, максимум {max_sources}):
   - Если вопрос касается научно обоснованных тем, можешь привести ссылки на SoTA исследования
   - Указывай только проверенные источники: научные статьи, книги признанных авторов
   - Формат: "Согласно исследованию [Автор, Год]..." или в конце ответа
   - НЕ выдумывай источники — лучше не указывать, чем указать несуществующий
   - Приводи источники только если они ТОЧНО релевантны вопросу"""

    system_prompt = f"""Ты — дружелюбный наставник по системному мышлению и личному развитию.
Отвечаешь на вопросы пользователя {name}.{occupation_info}{context_info}

ПРАВИЛА:
1. Отвечай кратко и по существу (3-5 абзацев максимум)
2. Используй простой язык, избегай академического стиля
3. Если вопрос связан с материалами Aisystant - опирайся на контекст
4. Если контекста недостаточно - честно скажи об этом
5. Если вопрос не по теме системного мышления - вежливо перенаправь
{sources_instruction}
{mcp_section}"""

    user_prompt = f"Вопрос: {question}"

    # Генерируем ответ
    answer = await claude.generate(system_prompt, user_prompt)

    if not answer:
        answer = f"К сожалению, {name}, не удалось получить ответ. Попробуйте переформулировать вопрос или спросить позже."

    return answer


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

    occupation_info = f"\nПрофессия: {occupation}" if occupation else ""
    context_section = f"\n\nКОНТЕКСТ:\n{additional_context}" if additional_context else ""

    system_prompt = f"""Ты — дружелюбный наставник по системному мышлению.
Отвечаешь на вопрос пользователя {name}.{occupation_info}

Отвечай кратко и по существу.{context_section}"""

    user_prompt = f"Вопрос: {question}"

    answer = await claude.generate(system_prompt, user_prompt)
    return answer or "Не удалось получить ответ. Попробуйте позже."
