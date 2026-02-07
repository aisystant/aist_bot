"""
Планировщик недельных тем для режима Лента.

Генерирует персонализированные предложения тем на неделю
на основе профиля пользователя и истории обучения.
"""

from typing import List, Dict, Optional
import json
import asyncio

from config import get_logger, FEED_TOPICS_TO_SUGGEST, ONTOLOGY_RULES, ONTOLOGY_RULES_TOPICS
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

    # Определяем язык ответа
    lang = intern.get('language', 'ru')
    lang_instruction = {
        'ru': "ВАЖНО: Пиши ВСЁ на русском языке.",
        'en': "IMPORTANT: Write EVERYTHING in English.",
        'es': "IMPORTANTE: Escribe TODO en español.",
        'fr': "IMPORTANT: Écris TOUT en français."
    }.get(lang, "IMPORTANT: Write EVERYTHING in English.")

    lang_reminder = {
        'ru': "НАПОМИНАНИЕ: Весь текст (title, why) должен быть на РУССКОМ языке!",
        'en': "REMINDER: All text (title, why) must be in ENGLISH!",
        'es': "RECORDATORIO: ¡Todo el texto (title, why) debe estar en ESPAÑOL!",
        'fr': "RAPPEL: Tout le texte (title, why) doit être en FRANÇAIS!"
    }.get(lang, "REMINDER: All text (title, why) must be in ENGLISH!")

    system_prompt = f"""Ты — персональный наставник по системному мышлению.
{lang_instruction}

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

{ONTOLOGY_RULES_TOPICS}

ФОРМАТ НАЗВАНИЯ ТЕМЫ:
- Максимум 5 слов
- Ёмко и конкретно

ФОРМАТ ОБОСНОВАНИЯ (why):
- Ровно одно предложение
- Объясни, почему полезно именно этому ученику

{f"АКТУАЛЬНЫЕ ТЕМЫ ИЗ МАТЕРИАЛОВ AISYSTANT:{chr(10)}{mcp_context}" if mcp_context else ""}

{lang_reminder}

Верни ответ СТРОГО в JSON формате:
[
    {{
        "title": "Topic name (max 5 words)",
        "why": "One sentence — why useful for this student.",
        "keywords": ["keyword1", "keyword2"]
    }},
    ...
]"""

    user_prompt = {
        'ru': f"Предложи {FEED_TOPICS_TO_SUGGEST} тем для изучения на неделю.",
        'en': f"Suggest {FEED_TOPICS_TO_SUGGEST} topics to study this week.",
        'es': f"Sugiere {FEED_TOPICS_TO_SUGGEST} temas para estudiar esta semana."
    }.get(lang, f"Предложи {FEED_TOPICS_TO_SUGGEST} тем для изучения на неделю.")

    response = await claude.generate(system_prompt, user_prompt)

    if not response:
        logger.error("Не удалось получить предложения тем от Claude")
        return get_fallback_topics(lang)

    # Парсим JSON из ответа
    topics = parse_topics_response(response)

    if not topics:
        logger.warning("Не удалось распарсить темы, используем fallback")
        return get_fallback_topics(lang)

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


def get_fallback_topics(lang: str = 'ru') -> List[Dict]:
    """Возвращает базовые темы если генерация не удалась"""
    fallback_topics = {
        'ru': [
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
        ],
        'en': [
            {
                "title": "Three States of Attention",
                "why": "Helps focus on what matters and get less distracted.",
                "keywords": ["attention", "awareness", "focus"],
            },
            {
                "title": "Work Product of Practice",
                "why": "Teaches to turn actions into concrete results.",
                "keywords": ["product", "result", "artifact"],
            },
            {
                "title": "Memes and Beliefs",
                "why": "Helps identify limiting beliefs in thinking.",
                "keywords": ["beliefs", "memes", "transformation"],
            },
            {
                "title": "Self-Engineering",
                "why": "Provides methods for conscious habit change.",
                "keywords": ["self-development", "methods", "habits"],
            },
            {
                "title": "Roles and Performers",
                "why": "Helps separate functions from specific people.",
                "keywords": ["roles", "functions", "performers"],
            },
        ],
        'es': [
            {
                "title": "Tres estados de atención",
                "why": "Ayuda a concentrarse en lo importante y distraerse menos.",
                "keywords": ["atención", "conciencia", "enfoque"],
            },
            {
                "title": "Producto de trabajo",
                "why": "Enseña a convertir acciones en resultados concretos.",
                "keywords": ["producto", "resultado", "artefacto"],
            },
            {
                "title": "Memes y creencias",
                "why": "Ayuda a identificar creencias limitantes en el pensamiento.",
                "keywords": ["creencias", "memes", "transformación"],
            },
            {
                "title": "Ingeniería personal",
                "why": "Proporciona métodos para cambiar hábitos conscientemente.",
                "keywords": ["autodesarrollo", "métodos", "hábitos"],
            },
            {
                "title": "Roles y ejecutores",
                "why": "Ayuda a separar funciones de personas específicas.",
                "keywords": ["roles", "funciones", "ejecutores"],
            },
        ],
        'fr': [
            {
                "title": "Trois états d'attention",
                "why": "Aide à se concentrer sur l'essentiel et à moins se distraire.",
                "keywords": ["attention", "conscience", "concentration"],
            },
            {
                "title": "Produit de travail",
                "why": "Apprend à transformer les actions en résultats concrets.",
                "keywords": ["produit", "résultat", "artefact"],
            },
            {
                "title": "Mèmes et croyances",
                "why": "Aide à identifier les croyances limitantes dans la pensée.",
                "keywords": ["croyances", "mèmes", "transformation"],
            },
            {
                "title": "Ingénierie de soi",
                "why": "Fournit des méthodes pour changer consciemment ses habitudes.",
                "keywords": ["développement personnel", "méthodes", "habitudes"],
            },
            {
                "title": "Rôles et exécutants",
                "why": "Aide à séparer les fonctions des personnes spécifiques.",
                "keywords": ["rôles", "fonctions", "exécutants"],
            },
        ],
    }
    return fallback_topics.get(lang, fallback_topics['en'])


async def generate_multi_topic_digest(
    topics: List[str],
    intern: dict,
    duration: int = 10,
    depth_level: int = 1
) -> Dict:
    """Генерирует дайджест по всем темам.

    Новая модель:
    - Дайджест включает ВСЕ выбранные темы (1-3)
    - Длительность делится между темами
    - Чем больше тем, тем меньше глубины на каждую
    - depth_level влияет на глубину раскрытия

    Args:
        topics: список названий тем (1-3 штуки)
        intern: профиль пользователя
        duration: общая длительность дайджеста в минутах
        depth_level: уровень глубины (1 = базовый, 2+ = глубже)

    Returns:
        {
            "intro": "вводный текст",
            "main_content": "основной контент по всем темам",
            "topics_list": ["тема1", "тема2"],
            "reflection_prompt": "вопрос для рефлексии",
            "depth_level": 1
        }
    """
    name = intern.get('name', 'пользователь')
    occupation = intern.get('occupation', '')

    topics_count = len(topics)
    if topics_count == 0:
        return {
            "intro": "Темы не выбраны",
            "main_content": "Используйте меню тем для выбора.",
            "topics_list": [],
            "reflection_prompt": "",
            "depth_level": depth_level,
        }

    # Расчёт времени на каждую тему
    time_per_topic = duration // topics_count
    words_per_topic = time_per_topic * 100  # ~100 слов в минуту чтения

    # Получаем контекст из MCP для всех тем — ПАРАЛЛЕЛЬНО
    mcp_context = ""

    async def fetch_topic_context(topic: str) -> str:
        """Получает контекст для одной темы из MCP (параллельно)."""
        context = ""
        try:
            # Запускаем оба поиска параллельно для каждой темы
            guides_task = mcp_guides.semantic_search(topic, limit=1)
            knowledge_task = mcp_knowledge.search(topic, limit=1)

            guides_results, knowledge_results = await asyncio.gather(
                guides_task, knowledge_task, return_exceptions=True
            )

            # Обрабатываем результаты guides
            if isinstance(guides_results, list):
                for item in guides_results:
                    if isinstance(item, dict):
                        text = item.get('text', item.get('content', ''))[:500]
                        if text:
                            context += f"\n[{topic}]: {text}"

            # Обрабатываем результаты knowledge
            if isinstance(knowledge_results, list):
                for item in knowledge_results:
                    if isinstance(item, dict):
                        text = item.get('text', item.get('content', ''))[:500]
                        if text:
                            context += f"\n[{topic}]: {text}"

        except Exception as e:
            logger.error(f"MCP search error for '{topic}': {e}")
        return context

    # Запускаем все темы параллельно с таймаутом 30 сек на MCP-фазу
    try:
        context_tasks = [fetch_topic_context(topic) for topic in topics]
        results = await asyncio.wait_for(
            asyncio.gather(*context_tasks, return_exceptions=True),
            timeout=30  # 30 сек на все MCP запросы
        )
        mcp_context = "".join(r for r in results if isinstance(r, str))
    except asyncio.TimeoutError:
        logger.warning("MCP context fetch timeout, continuing without context")
        mcp_context = ""

    # Описание уровня глубины
    depth_descriptions = {
        1: "базовое введение в тему, основные понятия",
        2: "практические примеры и применение",
        3: "связи между темами, нюансы",
        4: "глубокий анализ, неочевидные аспекты",
        5: "экспертный уровень, сложные случаи",
    }
    depth_desc = depth_descriptions.get(
        min(depth_level, 5),
        f"экспертный уровень (глубина {depth_level})"
    )

    topics_str = ", ".join(topics)

    # Определяем язык пользователя
    lang = intern.get('language', 'ru')
    lang_instruction = {
        'ru': "ВАЖНО: Пиши ВСЁ на русском языке.",
        'en': "IMPORTANT: Write EVERYTHING in English.",
        'es': "IMPORTANTE: Escribe TODO en español.",
        'fr': "IMPORTANT: Écris TOUT en français."
    }.get(lang, "IMPORTANT: Write EVERYTHING in English.")

    lang_reminder = {
        'ru': "НАПОМИНАНИЕ: Весь текст (intro, main_content, reflection_prompt) должен быть на РУССКОМ языке!",
        'en': "REMINDER: All text (intro, main_content, reflection_prompt) must be in ENGLISH!",
        'es': "RECORDATORIO: ¡Todo el texto (intro, main_content, reflection_prompt) debe estar en ESPAÑOL!",
        'fr': "RAPPEL: Tout le texte (intro, main_content, reflection_prompt) doit être en FRANÇAIS!"
    }.get(lang, "REMINDER: All text (intro, main_content, reflection_prompt) must be in ENGLISH!")

    system_prompt = f"""Ты — персональный наставник по системному мышлению.
Создай дайджест, объединяющий несколько тем для {name}.
{lang_instruction}

ПРОФИЛЬ:
- Занятие: {occupation or 'не указано'}

ТЕМЫ ДАЙДЖЕСТА ({topics_count} шт.):
{chr(10).join(f'- {t}' for t in topics)}

УРОВЕНЬ ГЛУБИНЫ: {depth_level} — {depth_desc}
(С каждым днём одни и те же темы раскрываются глубже)

ФОРМАТ:
1. Краткое введение (1-2 предложения) — зацепи внимание, объедини темы
2. По каждой теме ~{words_per_topic} слов — раскрой на текущем уровне глубины
3. Покажи связи между темами если они есть
4. Один общий вопрос для рефлексии в конце

{f"КОНТЕКСТ ИЗ МАТЕРИАЛОВ:{chr(10)}{mcp_context[:3000]}" if mcp_context else ""}

ВАЖНО:
- Пиши просто и вовлекающе
- Используй примеры из сферы "{occupation}" если возможно
- НЕ используй заголовки, подзаголовки и markdown-разметку
- Переходи от темы к теме плавно, без явного деления
- Заверши текст вопросом для размышления

{lang_reminder}

Верни JSON:
{{
    "intro": "краткое введение (1-2 предложения)",
    "main_content": "основной текст по всем темам",
    "reflection_prompt": "один вопрос для рефлексии"
}}"""

    user_prompt = {
        'ru': f"Темы: {topics_str}\nУровень глубины: {depth_level}",
        'en': f"Topics: {topics_str}\nDepth level: {depth_level}",
        'es': f"Temas: {topics_str}\nNivel de profundidad: {depth_level}"
    }.get(lang, f"Темы: {topics_str}\nУровень глубины: {depth_level}")

    response = await claude.generate(system_prompt, user_prompt)

    if not response:
        return {
            "intro": f"Сегодняшний дайджест: {topics_str}",
            "main_content": "Контент не удалось сгенерировать. Попробуйте позже.",
            "topics_list": topics,
            "reflection_prompt": "Какие мысли вызвали эти темы?",
            "depth_level": depth_level,
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
                "topics_list": topics,
                "reflection_prompt": content.get('reflection_prompt', ''),
                "depth_level": depth_level,
            }
    except Exception as e:
        logger.error(f"Multi-topic content parse error: {e}")

    # Fallback: используем весь ответ как контент
    return {
        "intro": f"Дайджест: {topics_str}",
        "main_content": response,
        "topics_list": topics,
        "reflection_prompt": "Что вы вынесли из этого материала?",
        "depth_level": depth_level,
    }


async def generate_topic_content(
    topic: Dict,
    intern: dict,
    session_duration: int = 7
) -> Dict:
    """Генерирует контент для одной темы (legacy, для обратной совместимости)

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

    # Определяем язык ответа
    lang = intern.get('language', 'ru')
    lang_instruction = {
        'ru': "ВАЖНО: Пиши ВСЁ на русском языке.",
        'en': "IMPORTANT: Write EVERYTHING in English.",
        'es': "IMPORTANTE: Escribe TODO en español.",
        'fr': "IMPORTANT: Écris TOUT en français."
    }.get(lang, "IMPORTANT: Write EVERYTHING in English.")

    lang_reminder = {
        'ru': "НАПОМИНАНИЕ: Весь текст (intro, main_content, reflection_prompt) должен быть на РУССКОМ языке!",
        'en': "REMINDER: All text (intro, main_content, reflection_prompt) must be in ENGLISH!",
        'es': "RECORDATORIO: ¡Todo el texto (intro, main_content, reflection_prompt) debe estar en ESPAÑOL!",
        'fr': "RAPPEL: Tout le texte (intro, main_content, reflection_prompt) doit être en FRANÇAIS!"
    }.get(lang, "REMINDER: All text (intro, main_content, reflection_prompt) must be in ENGLISH!")

    system_prompt = f"""Ты — персональный наставник по системному мышлению.
{lang_instruction}

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

{ONTOLOGY_RULES}

{lang_reminder}

Верни JSON:
{{
    "intro": "краткое введение",
    "main_content": "основной текст",
    "reflection_prompt": "вопрос для рефлексии"
}}"""

    user_prompt = {
        'ru': f"Тема: {topic.get('title')}\nОписание: {topic.get('description', '')}",
        'en': f"Topic: {topic.get('title')}\nDescription: {topic.get('description', '')}",
        'es': f"Tema: {topic.get('title')}\nDescripción: {topic.get('description', '')}"
    }.get(lang, f"Тема: {topic.get('title')}\nОписание: {topic.get('description', '')}")

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
