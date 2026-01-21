"""
Планировщик недельных тем для режима Лента.

Генерирует персонализированные предложения тем на неделю
на основе профиля пользователя и истории обучения.
"""

from typing import List, Dict, Optional
import json

from config import get_logger, FEED_TOPICS_TO_SUGGEST
from clients import claude, mcp_guides, mcp_knowledge

logger = get_logger(__name__)


async def suggest_weekly_topics(intern: dict) -> List[Dict]:
    """Генерирует предложения тем на неделю

    Args:
        intern: профиль пользователя

    Returns:
        Список тем с описаниями:
        [
            {"title": "Системное мышление", "description": "...", "why": "..."},
            ...
        ]
    """
    name = intern.get('name', 'пользователь')
    occupation = intern.get('occupation', '')
    interests = intern.get('interests', [])
    goals = intern.get('goals', '')
    motivation = intern.get('motivation', '')

    # Получаем контекст из MCP для актуальных тем
    mcp_context = await get_trending_topics()

    # Формируем профиль для промпта
    interests_str = ', '.join(interests) if interests else 'не указаны'

    system_prompt = f"""Ты — персональный наставник по системному мышлению.
Твоя задача — предложить {FEED_TOPICS_TO_SUGGEST} тем для изучения.

ПРОФИЛЬ УЧЕНИКА:
- Имя: {name}
- Занятие: {occupation or 'не указано'}
- Интересы: {interests_str}
- Цели: {goals or 'не указаны'}
- Мотивация: {motivation or 'не указана'}

ПРАВИЛА:
1. Темы из области системного мышления и личного развития
2. Учитывай профессию и интересы — темы должны быть релевантны
3. Каждая тема для 5-12 минут изучения
4. Разнообразь темы — не повторяй похожие концепции

ФОРМАТ НАЗВАНИЯ ТЕМЫ:
- Максимум 5 слов
- Ёмко и конкретно
- Примеры: "Три состояния внимания", "Рабочий продукт практики", "Инженерия себя"

ФОРМАТ ОБОСНОВАНИЯ (why):
- Ровно одно предложение
- Объясни, почему полезно именно этому ученику
- Пример: "Поможет структурировать хаос задач в твоей работе менеджера."

{f"АКТУАЛЬНЫЕ ТЕМЫ ИЗ МАТЕРИАЛОВ AISYSTANT:{chr(10)}{mcp_context}" if mcp_context else ""}

Верни ответ СТРОГО в JSON формате:
[
    {{
        "title": "Название (до 5 слов)",
        "why": "Одно предложение — почему полезно именно тебе.",
        "keywords": ["ключевое", "слово"]
    }},
    ...
]"""

    user_prompt = f"Предложи {FEED_TOPICS_TO_SUGGEST} темы для изучения на неделю."

    response = await claude.generate(system_prompt, user_prompt)

    if not response:
        logger.error("Не удалось получить предложения тем от Claude")
        return get_fallback_topics()

    # Парсим JSON из ответа
    topics = parse_topics_response(response)

    if not topics:
        logger.warning("Не удалось распарсить темы, используем fallback")
        return get_fallback_topics()

    logger.info(f"Сгенерировано {len(topics)} тем для {name}")
    return topics


async def get_trending_topics() -> str:
    """Получает актуальные темы из MCP для контекста"""
    try:
        # Ищем свежие посты про системное мышление
        results = await mcp_knowledge.search(
            "системное мышление практики",
            limit=3
        )

        if results:
            topics = []
            for item in results:
                if isinstance(item, dict):
                    text = item.get('title', item.get('text', ''))[:200]
                    if text:
                        topics.append(f"- {text}")

            if topics:
                return "\n".join(topics[:5])
    except Exception as e:
        logger.error(f"Ошибка получения trending topics: {e}")

    return ""


def parse_topics_response(response: str) -> List[Dict]:
    """Парсит JSON ответ с темами

    Args:
        response: ответ от Claude

    Returns:
        Список тем или пустой список при ошибке
    """
    # Пробуем найти JSON в ответе
    try:
        # Ищем JSON массив в ответе
        start = response.find('[')
        end = response.rfind(']') + 1

        if start >= 0 and end > start:
            json_str = response[start:end]
            topics = json.loads(json_str)

            # Валидируем структуру
            validated = []
            for topic in topics:
                if isinstance(topic, dict) and 'title' in topic:
                    title = topic.get('title', '')
                    # Обрезаем до 5 слов если длиннее
                    words = title.split()
                    if len(words) > 5:
                        title = ' '.join(words[:5])
                    validated.append({
                        'title': title,
                        'why': topic.get('why', ''),
                        'keywords': topic.get('keywords', []),
                    })

            return validated[:FEED_TOPICS_TO_SUGGEST]

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
    except Exception as e:
        logger.error(f"Parse error: {e}")

    return []


def get_fallback_topics() -> List[Dict]:
    """Возвращает базовые темы если генерация не удалась"""
    return [
        {
            "title": "Три состояния внимания",
            "why": "Поможет концентрироваться на важном и меньше отвлекаться.",
            "keywords": ["внимание", "осознанность", "фокус"],
        },
        {
            "title": "Рабочий продукт практики",
            "why": "Научит превращать действия в конкретные результаты.",
            "keywords": ["продукт", "результат", "артефакт"],
        },
        {
            "title": "Мемы и убеждения",
            "why": "Поможет выявить ограничивающие установки в мышлении.",
            "keywords": ["убеждения", "мемы", "трансформация"],
        },
        {
            "title": "Инженерия себя",
            "why": "Даст методы для осознанного изменения привычек.",
            "keywords": ["саморазвитие", "методы", "привычки"],
        },
        {
            "title": "Роли и исполнители",
            "why": "Поможет разделять функции и конкретных людей.",
            "keywords": ["роли", "функции", "исполнители"],
        },
    ]


async def generate_topic_content(
    topic: Dict,
    intern: dict,
    session_duration: int = 7
) -> Dict:
    """Генерирует контент для темы сессии

    Args:
        topic: тема с title, description, keywords
        intern: профиль пользователя
        session_duration: длительность сессии в минутах (5-12)

    Returns:
        Словарь с контентом:
        {
            "intro": "вводный текст",
            "main_content": "основной контент",
            "reflection_prompt": "вопрос для рефлексии",
            "fixation_prompt": "промпт для фиксации"
        }
    """
    name = intern.get('name', 'пользователь')
    occupation = intern.get('occupation', '')

    # Получаем контекст из MCP по ключевым словам темы
    keywords = topic.get('keywords', [])
    search_query = ' '.join(keywords) if keywords else topic.get('title', '')

    mcp_context = ""
    try:
        # Ищем в руководствах
        guides_results = await mcp_guides.semantic_search(search_query, limit=2)
        if guides_results:
            for item in guides_results:
                if isinstance(item, dict):
                    text = item.get('text', item.get('content', ''))[:1000]
                    if text:
                        mcp_context += f"\n\n{text}"

        # Ищем в базе знаний
        knowledge_results = await mcp_knowledge.search(search_query, limit=2)
        if knowledge_results:
            for item in knowledge_results:
                if isinstance(item, dict):
                    text = item.get('text', item.get('content', ''))[:1000]
                    if text:
                        mcp_context += f"\n\n{text}"
    except Exception as e:
        logger.error(f"MCP search error: {e}")

    # Рассчитываем объём текста
    words = session_duration * 100  # ~100 слов в минуту чтения

    system_prompt = f"""Ты — персональный наставник по системному мышлению.
Создай микро-урок на тему "{topic.get('title')}" для {name}.

ПРОФИЛЬ:
- Занятие: {occupation or 'не указано'}

ФОРМАТ:
1. Краткое введение (1-2 предложения) — зацепи внимание
2. Основной контент (~{words} слов) — раскрой тему простым языком с примерами
3. Вопрос для рефлексии — один открытый вопрос

{f"КОНТЕКСТ ИЗ МАТЕРИАЛОВ:{chr(10)}{mcp_context[:3000]}" if mcp_context else ""}

ВАЖНО:
- Пиши просто и вовлекающе
- Используй примеры из сферы "{occupation}" если возможно
- Не используй заголовки и markdown
- Заверши текст вопросом для размышления

Верни JSON:
{{
    "intro": "краткое введение",
    "main_content": "основной текст",
    "reflection_prompt": "вопрос для рефлексии"
}}"""

    user_prompt = f"Тема: {topic.get('title')}\nОписание: {topic.get('description', '')}"

    response = await claude.generate(system_prompt, user_prompt)

    if not response:
        return {
            "intro": f"Сегодня поговорим о теме: {topic.get('title')}",
            "main_content": topic.get('description', 'Контент не удалось сгенерировать.'),
            "reflection_prompt": "Как эта тема связана с вашей жизнью?",
        }

    # Парсим JSON
    try:
        start = response.find('{')
        end = response.rfind('}') + 1
        if start >= 0 and end > start:
            content = json.loads(response[start:end])
            return {
                "intro": content.get('intro', ''),
                "main_content": content.get('main_content', ''),
                "reflection_prompt": content.get('reflection_prompt', ''),
            }
    except Exception as e:
        logger.error(f"Content parse error: {e}")

    # Fallback: используем весь ответ как контент
    return {
        "intro": "",
        "main_content": response,
        "reflection_prompt": "Что вы вынесли из этого материала?",
    }
