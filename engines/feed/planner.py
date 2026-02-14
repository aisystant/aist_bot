"""
Планировщик недельных тем для режима Лента.

Темы берутся из каталога руководств программы «Личное развитие»,
а не генерируются Claude произвольно. Claude используется только
для персонализации обоснования (why) под конкретного пользователя.

Каталог = секции 3 руководств ЛР:
  1-1: Системное саморазвитие
  1-2: Практики саморазвития
  1-3: Введение в системное мышление
"""

from typing import List, Dict, Optional
import json
import asyncio
import random

from config import get_logger, FEED_TOPICS_TO_SUGGEST, ONTOLOGY_RULES, ONTOLOGY_RULES_TOPICS
from clients import claude, mcp_knowledge

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Каталог тем из руководств программы «Личное развитие»
# ---------------------------------------------------------------------------
# Каждая тема привязана к guide_slug + section (для MCP-поиска контента).
# assessment_priority: chaos=0-3, deadlock=0-3, turning_point=0-3
#   3 = максимальный приоритет для данного состояния
# ---------------------------------------------------------------------------
GUIDE_TOPIC_CATALOG: List[Dict] = [
    # === Guide 1-1: Системное саморазвитие ===
    {
        "title": "Ментальное пространство",
        "guide": "1-1-self-development",
        "section": "01-physical-world-and-mental-space",
        "keywords": ["ментальное пространство", "теория", "модели реальности"],
        "chaos": 1, "deadlock": 2, "turning_point": 3,
    },
    {
        "title": "Техноэволюция и создатель",
        "guide": "1-1-self-development",
        "section": "01-physical-world-and-mental-space",
        "keywords": ["техноэволюция", "создатель", "изменение мира"],
        "chaos": 0, "deadlock": 1, "turning_point": 3,
    },
    {
        "title": "Обучение и знания",
        "guide": "1-1-self-development",
        "section": "02-training-and-time",
        "keywords": ["обучение", "знания", "мировоззрение"],
        "chaos": 2, "deadlock": 2, "turning_point": 2,
    },
    {
        "title": "Интеллект-стек",
        "guide": "1-1-self-development",
        "section": "02-training-and-time",
        "keywords": ["интеллект-стек", "мышление", "компетенции"],
        "chaos": 1, "deadlock": 2, "turning_point": 3,
    },
    {
        "title": "Инвестирование времени",
        "guide": "1-1-self-development",
        "section": "02-training-and-time",
        "keywords": ["время", "инвестирование", "приоритеты"],
        "chaos": 3, "deadlock": 2, "turning_point": 1,
    },
    {
        "title": "Собранность и внимание",
        "guide": "1-1-self-development",
        "section": "03-composure-and-attention",
        "keywords": ["собранность", "внимание", "фокус"],
        "chaos": 3, "deadlock": 2, "turning_point": 1,
    },
    {
        "title": "Важное, текущее и срочное",
        "guide": "1-1-self-development",
        "section": "03-composure-and-attention",
        "keywords": ["приоритизация", "важное", "срочное", "текущее"],
        "chaos": 3, "deadlock": 1, "turning_point": 1,
    },
    {
        "title": "Потеря внимания",
        "guide": "1-1-self-development",
        "section": "03-composure-and-attention",
        "keywords": ["потеря внимания", "отвлечения", "концентрация"],
        "chaos": 3, "deadlock": 1, "turning_point": 0,
    },
    {
        "title": "Неудовлетворённость как двигатель",
        "guide": "1-1-self-development",
        "section": "04-systematic-approach-in-personality-psychology",
        "keywords": ["неудовлетворённость", "мотивация", "изменения"],
        "chaos": 1, "deadlock": 3, "turning_point": 2,
    },
    {
        "title": "Состояния человека",
        "guide": "1-1-self-development",
        "section": "04-systematic-approach-in-personality-psychology",
        "keywords": ["состояния", "психология", "системный подход"],
        "chaos": 2, "deadlock": 2, "turning_point": 2,
    },
    {
        "title": "Эмоции и эмоциональная стабильность",
        "guide": "1-1-self-development",
        "section": "04-systematic-approach-in-personality-psychology",
        "keywords": ["эмоции", "стабильность", "чувства", "развитие"],
        "chaos": 3, "deadlock": 2, "turning_point": 1,
    },
    {
        "title": "Роли и ролевое мастерство",
        "guide": "1-1-self-development",
        "section": "05-role-role-mastery-and-method",
        "keywords": ["роли", "мастерство", "метод", "практика"],
        "chaos": 1, "deadlock": 3, "turning_point": 2,
    },
    {
        "title": "Ролевые интересы",
        "guide": "1-1-self-development",
        "section": "05-role-role-mastery-and-method",
        "keywords": ["ролевые интересы", "предпочтения", "удовлетворение"],
        "chaos": 0, "deadlock": 2, "turning_point": 3,
    },
    {
        "title": "Стадии и методы саморазвития",
        "guide": "1-1-self-development",
        "section": "05-role-role-mastery-and-method",
        "keywords": ["стадии", "методы", "саморазвитие", "практики"],
        "chaos": 2, "deadlock": 3, "turning_point": 2,
    },
    {
        "title": "Понятия системного мышления",
        "guide": "1-1-self-development",
        "section": "06-what-is-systems-thinking",
        "keywords": ["системное мышление", "понятия", "подход"],
        "chaos": 1, "deadlock": 2, "turning_point": 3,
    },
    {
        "title": "Понятие системы",
        "guide": "1-1-self-development",
        "section": "06-what-is-systems-thinking",
        "keywords": ["система", "определение", "системный подход"],
        "chaos": 1, "deadlock": 2, "turning_point": 3,
    },
    {
        "title": "Инженерия и предпринимательство",
        "guide": "1-1-self-development",
        "section": "07-engineering-management-entrepreneurship",
        "keywords": ["инженерия", "менеджмент", "предпринимательство"],
        "chaos": 0, "deadlock": 2, "turning_point": 3,
    },
    {
        "title": "Личность и ИИ-агент",
        "guide": "1-1-self-development",
        "section": "08-personality-and-agent-human-and-ai",
        "keywords": ["личность", "агент", "ИИ", "свобода воли"],
        "chaos": 1, "deadlock": 2, "turning_point": 2,
    },
    {
        "title": "Причины выгорания и депрессии",
        "guide": "1-1-self-development",
        "section": "08-personality-and-agent-human-and-ai",
        "keywords": ["выгорание", "депрессия", "беспокойство"],
        "chaos": 3, "deadlock": 2, "turning_point": 0,
    },
    {
        "title": "Траектория развития личности",
        "guide": "1-1-self-development",
        "section": "09-personal-development-trajectory",
        "keywords": ["траектория", "развитие", "цели", "успех"],
        "chaos": 0, "deadlock": 2, "turning_point": 3,
    },
    {
        "title": "Непрерывное развитие",
        "guide": "1-1-self-development",
        "section": "09-personal-development-trajectory",
        "keywords": ["непрерывное развитие", "бесконечное", "рост"],
        "chaos": 0, "deadlock": 3, "turning_point": 3,
    },
    # === Guide 1-2: Практики саморазвития ===
    {
        "title": "Роль ученика",
        "guide": "1-2-self-development-methods",
        "section": "01-about-self-development-practices-and-the-second-brain",
        "keywords": ["ученик", "второй мозг", "практики саморазвития"],
        "chaos": 2, "deadlock": 3, "turning_point": 1,
    },
    {
        "title": "Учёт и инвестирование времени",
        "guide": "1-2-self-development-methods",
        "section": "02-investing-and-time-tracking",
        "keywords": ["учёт времени", "инвестирование", "тайм-менеджмент"],
        "chaos": 3, "deadlock": 2, "turning_point": 1,
    },
    {
        "title": "Систематическое медленное чтение",
        "guide": "1-2-self-development-methods",
        "section": "03-systematic-slow-reading",
        "keywords": ["медленное чтение", "потребление информации", "осознанность"],
        "chaos": 2, "deadlock": 3, "turning_point": 1,
    },
    {
        "title": "Мышление письмом",
        "guide": "1-2-self-development-methods",
        "section": "04-thinking-in-writing",
        "keywords": ["мышление письмом", "мозг", "фиксация мыслей"],
        "chaos": 2, "deadlock": 3, "turning_point": 2,
    },
    {
        "title": "Мышление проговариванием",
        "guide": "1-2-self-development-methods",
        "section": "05-thinking-by-speaking",
        "keywords": ["проговаривание", "мышление", "коммуникация"],
        "chaos": 1, "deadlock": 2, "turning_point": 2,
    },
    {
        "title": "Организация досуга",
        "guide": "1-2-self-development-methods",
        "section": "06-leisure-organization",
        "keywords": ["досуг", "энергия", "выгорание", "отдых"],
        "chaos": 3, "deadlock": 1, "turning_point": 1,
    },
    {
        "title": "Формирование окружения",
        "guide": "1-2-self-development-methods",
        "section": "07-formation-of-the-environment",
        "keywords": ["окружение", "среда", "мотивация", "влияние"],
        "chaos": 1, "deadlock": 2, "turning_point": 3,
    },
    {
        "title": "Стратегирование",
        "guide": "1-2-self-development-methods",
        "section": "08-strategizing",
        "keywords": ["стратегия", "проекты", "неудовлетворённости"],
        "chaos": 0, "deadlock": 2, "turning_point": 3,
    },
    {
        "title": "Планирование",
        "guide": "1-2-self-development-methods",
        "section": "09-planning",
        "keywords": ["планирование", "бюджет времени", "распорядок"],
        "chaos": 3, "deadlock": 2, "turning_point": 1,
    },
    # === Guide 1-3: Введение в системное мышление ===
    {
        "title": "Зачем системное мышление",
        "guide": "1-3-systems-thinking-introduction",
        "section": "01-what-is-systems-thinking-and-why-does-modern-man-need-it",
        "keywords": ["системное мышление", "зачем", "современный мир"],
        "chaos": 1, "deadlock": 2, "turning_point": 3,
    },
    {
        "title": "Воплощение и описание системы",
        "guide": "1-3-systems-thinking-introduction",
        "section": "02-implementation-and-description-of-the-system",
        "keywords": ["воплощение", "описание", "физический мир"],
        "chaos": 0, "deadlock": 2, "turning_point": 2,
    },
    {
        "title": "Роли и успешная система",
        "guide": "1-3-systems-thinking-introduction",
        "section": "03-roles-and-a-successful-system",
        "keywords": ["роли", "успешная система", "исполнители"],
        "chaos": 1, "deadlock": 3, "turning_point": 2,
    },
    {
        "title": "Виды систем",
        "guide": "1-3-systems-thinking-introduction",
        "section": "04-types-of-systems-target-system-supersystem-creation-systems-and-others",
        "keywords": ["целевая система", "надсистема", "системы создания"],
        "chaos": 0, "deadlock": 1, "turning_point": 3,
    },
    {
        "title": "Системные уровни и цепочки",
        "guide": "1-3-systems-thinking-introduction",
        "section": "05-system-levels-creation-chains-areas-of-interest",
        "keywords": ["системные уровни", "цепочки создания", "области интересов"],
        "chaos": 0, "deadlock": 1, "turning_point": 3,
    },
    {
        "title": "Создание и развитие",
        "guide": "1-3-systems-thinking-introduction",
        "section": "07-creation-and-development",
        "keywords": ["создание", "развитие", "стадии", "управление"],
        "chaos": 1, "deadlock": 2, "turning_point": 3,
    },
]


def _select_topics_from_catalog(
    assessment_state: str,
    count: int = FEED_TOPICS_TO_SUGGEST,
    exclude_titles: Optional[List[str]] = None,
) -> List[Dict]:
    """Выбирает темы из каталога с учётом персонализации.

    Алгоритм:
    1. Берём приоритет из assessment_state (chaos/deadlock/turning_point)
    2. Добавляем случайность (±random) для разнообразия
    3. Исключаем уже показанные темы
    4. Возвращаем top-N
    """
    exclude = set(exclude_titles or [])
    candidates = [t for t in GUIDE_TOPIC_CATALOG if t["title"] not in exclude]

    if not candidates:
        # Все темы уже показаны — сбрасываем фильтр
        candidates = list(GUIDE_TOPIC_CATALOG)

    # Оценка: приоритет по assessment_state + случайность
    state_key = assessment_state if assessment_state in ('chaos', 'deadlock', 'turning_point') else 'deadlock'

    scored = []
    for topic in candidates:
        priority = topic.get(state_key, 1)
        # Добавляем случайность: ±1.5 чтобы не было детерминированного порядка
        noise = random.uniform(-1.5, 1.5)
        scored.append((priority + noise, topic))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Берём top-N, но из разных guide (для разнообразия)
    selected = []
    used_sections = set()
    for _, topic in scored:
        section_key = f"{topic['guide']}:{topic['section']}"
        if section_key in used_sections:
            continue
        selected.append(topic)
        used_sections.add(section_key)
        if len(selected) >= count:
            break

    # Если не хватило (из-за фильтра по секциям) — добираем
    if len(selected) < count:
        for _, topic in scored:
            if topic not in selected:
                selected.append(topic)
                if len(selected) >= count:
                    break

    return selected


async def suggest_weekly_topics(intern: dict) -> List[Dict]:
    """Предлагает темы на неделю из каталога руководств ЛР.

    Темы выбираются из GUIDE_TOPIC_CATALOG с учётом assessment_state,
    затем Claude генерирует персонализированное обоснование (why).

    Args:
        intern: профиль пользователя

    Returns:
        Список тем: [{"title": "...", "why": "...", "keywords": [...]}]
    """
    name = intern.get('name', 'пользователь')
    occupation = intern.get('occupation', '')
    interests = intern.get('interests', [])
    goals = intern.get('goals', '')
    assessment_state = intern.get('assessment_state', '')
    lang = intern.get('language', 'ru')

    # 1. Выбираем темы из каталога
    selected = _select_topics_from_catalog(assessment_state)

    # 2. Просим Claude персонализировать обоснования (why)
    topics_for_prompt = "\n".join(
        f"- {t['title']} (ключевые слова: {', '.join(t['keywords'])})"
        for t in selected
    )
    interests_str = ', '.join(interests) if interests else 'не указаны'

    lang_instruction = {
        'ru': "ВАЖНО: Пиши ВСЁ на русском языке.",
        'en': "IMPORTANT: Write EVERYTHING in English.",
        'es': "IMPORTANTE: Escribe TODO en español.",
        'fr': "IMPORTANT: Écris TOUT en français.",
        'zh': "重要：请用中文书写所有内容。"
    }.get(lang, "IMPORTANT: Write EVERYTHING in English.")

    system_prompt = f"""Ты — персональный наставник.
{lang_instruction}

ЗАДАЧА: Для каждой темы из списка напиши одно предложение — почему
эта тема полезна именно этому ученику. Учитывай профиль.

ПРОФИЛЬ УЧЕНИКА:
- Имя: {name}
- Занятие: {occupation or 'не указано'}
- Интересы: {interests_str}
- Цели: {goals or 'не указаны'}

ТЕМЫ:
{topics_for_prompt}

Верни СТРОГО JSON массив (порядок тем сохрани):
[
    {{"title": "название темы (как в списке)", "why": "одно предложение"}},
    ...
]"""

    response = await claude.generate(system_prompt, "Персонализируй обоснования тем.")

    # 3. Парсим ответ Claude
    personalized = _parse_why_response(response, selected)

    if personalized:
        logger.info(f"Каталог: {len(personalized)} тем для {name} (state={assessment_state})")
        return personalized

    # Fallback: возвращаем темы без персонализации
    logger.warning("Claude не вернул персонализацию, используем каталог as-is")
    return _catalog_to_topics(selected, lang)


def _parse_why_response(response: Optional[str], selected: List[Dict]) -> List[Dict]:
    """Парсит ответ Claude с персонализированными why."""
    if not response:
        return []
    try:
        start = response.find('[')
        end = response.rfind(']') + 1
        if start >= 0 and end > start:
            items = json.loads(response[start:end])
            result = []
            for i, item in enumerate(items):
                if not isinstance(item, dict) or 'title' not in item:
                    continue
                # Берём keywords из каталога (Claude их не генерирует)
                catalog_entry = selected[i] if i < len(selected) else {}
                result.append({
                    'title': item.get('title', catalog_entry.get('title', '')),
                    'why': item.get('why', ''),
                    'keywords': catalog_entry.get('keywords', []),
                })
            return result[:FEED_TOPICS_TO_SUGGEST]
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Why parse error: {e}")
    return []


def _catalog_to_topics(selected: List[Dict], lang: str = 'ru') -> List[Dict]:
    """Конвертирует записи каталога в формат тем (без персонализации)."""
    default_why = {
        'ru': "Тема из программы «Личное развитие».",
        'en': "A topic from the Personal Development program.",
        'es': "Un tema del programa de Desarrollo Personal.",
        'fr': "Un sujet du programme de Développement Personnel.",
        'zh': '来自"个人发展"项目的主题。',
    }
    return [
        {
            'title': t['title'],
            'why': default_why.get(lang, default_why['en']),
            'keywords': t.get('keywords', []),
        }
        for t in selected[:FEED_TOPICS_TO_SUGGEST]
    ]


def get_fallback_topics(lang: str = 'ru') -> List[Dict]:
    """Возвращает темы из каталога если всё остальное не сработало."""
    # Берём 5 случайных тем с высоким приоритетом для deadlock (средний профиль)
    selected = _select_topics_from_catalog('deadlock', FEED_TOPICS_TO_SUGGEST)
    return _catalog_to_topics(selected, lang)


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
            "main_content": "fallback (конкатенация summary+detail)",
            "topics_detail": [{"title", "summary", "detail"}, ...],
            "topics_list": ["тема1", "тема2"],
            "reflection_prompt": "вопрос для рефлексии",
            "depth_level": 1
        }
    """
    name = intern.get('name', 'пользователь')
    occupation = intern.get('occupation', '')
    assessment_state = intern.get('assessment_state', '')

    topics_count = len(topics)
    if topics_count == 0:
        return {
            "intro": "Темы не выбраны",
            "main_content": "Используйте меню тем для выбора.",
            "topics_detail": [],
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
        """Получает контекст для одной темы из unified Knowledge MCP (guides)."""
        context = ""
        try:
            results = await mcp_knowledge.search(
                topic, limit=3, source_type="guides"
            )
            if isinstance(results, list):
                for item in results:
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
        'fr': "IMPORTANT: Écris TOUT en français.",
        'zh': "重要：请用中文书写所有内容。"
    }.get(lang, "IMPORTANT: Write EVERYTHING in English.")

    # Адаптация стиля дайджеста по состоянию теста
    assessment_digest_hint = _get_feed_digest_hint(assessment_state)

    lang_reminder = {
        'ru': "НАПОМИНАНИЕ: Весь текст (intro, topics[].summary, topics[].detail, reflection_prompt) должен быть на РУССКОМ языке!",
        'en': "REMINDER: All text (intro, topics[].summary, topics[].detail, reflection_prompt) must be in ENGLISH!",
        'es': "RECORDATORIO: ¡Todo el texto (intro, topics[].summary, topics[].detail, reflection_prompt) debe estar en ESPAÑOL!",
        'fr': "RAPPEL: Tout le texte (intro, topics[].summary, topics[].detail, reflection_prompt) doit être en FRANÇAIS!",
        'zh': "提醒：所有文本（intro、topics[].summary、topics[].detail、reflection_prompt）必须使用中文！"
    }.get(lang, "REMINDER: All text (intro, topics[].summary, topics[].detail, reflection_prompt) must be in ENGLISH!")

    system_prompt = f"""Ты — персональный наставник по системному мышлению.
Создай дайджест по нескольким темам для {name}.
{lang_instruction}

ПРОФИЛЬ:
- Занятие: {occupation or 'не указано'}
{assessment_digest_hint}
ТЕМЫ ДАЙДЖЕСТА ({topics_count} шт.):
{chr(10).join(f'- {t}' for t in topics)}

УРОВЕНЬ ГЛУБИНЫ: {depth_level} — {depth_desc}
(С каждым днём одни и те же темы раскрываются глубже)

ФОРМАТ:
1. Краткое введение (intro) — 1-2 предложения, зацепи внимание
2. По каждой теме два блока:
   - summary: 2-3 предложения — суть темы (показывается в списке)
   - detail: развёрнутый текст ~{words_per_topic} слов — примеры, применение, глубина
3. Один общий вопрос для рефлексии в конце

{f"КОНТЕКСТ ИЗ МАТЕРИАЛОВ:{chr(10)}{mcp_context[:3000]}" if mcp_context else ""}

ВАЖНО:
- Пиши просто и вовлекающе
- Используй примеры из сферы "{occupation}" если возможно
- В detail НЕ используй markdown-заголовки (# ##), можно *жирный* и _курсив_
- Каждую тему обрабатывай отдельно
- title должен совпадать с названием темы из списка

{lang_reminder}

Верни JSON:
{{
    "intro": "краткое введение (1-2 предложения)",
    "topics": [
        {{"title": "название темы 1", "summary": "2-3 предложения — суть", "detail": "развёрнутый текст"}},
        {{"title": "название темы 2", "summary": "2-3 предложения — суть", "detail": "развёрнутый текст"}}
    ],
    "reflection_prompt": "один вопрос для рефлексии"
}}"""

    user_prompt = {
        'ru': f"Темы: {topics_str}\nУровень глубины: {depth_level}",
        'en': f"Topics: {topics_str}\nDepth level: {depth_level}",
        'es': f"Temas: {topics_str}\nNivel de profundidad: {depth_level}",
        'zh': f"主题：{topics_str}\n深度级别：{depth_level}"
    }.get(lang, f"Темы: {topics_str}\nУровень глубины: {depth_level}")

    response = await claude.generate(system_prompt, user_prompt)

    if not response:
        return {
            "intro": f"Сегодняшний дайджест: {topics_str}",
            "main_content": "Контент не удалось сгенерировать. Попробуйте позже.",
            "topics_detail": [],
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
            topics_detail = content.get('topics', [])
            # Backward-compat fallback: собираем main_content из per-topic блоков
            if topics_detail:
                main_content = "\n\n".join(
                    f"*{td.get('title', '')}*\n{td.get('summary', '')}\n{td.get('detail', '')}"
                    for td in topics_detail
                )
            else:
                main_content = content.get('main_content', '')
            return {
                "intro": content.get('intro', ''),
                "main_content": main_content,
                "topics_detail": topics_detail,
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
        "topics_detail": [],
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
        results = await mcp_knowledge.search(
            search_query, limit=4, source_type="guides"
        )
        if results:
            for item in results:
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
        'fr': "IMPORTANT: Écris TOUT en français.",
        'zh': "重要：请用中文书写所有内容。"
    }.get(lang, "IMPORTANT: Write EVERYTHING in English.")

    lang_reminder = {
        'ru': "НАПОМИНАНИЕ: Весь текст (intro, main_content, reflection_prompt) должен быть на РУССКОМ языке!",
        'en': "REMINDER: All text (intro, main_content, reflection_prompt) must be in ENGLISH!",
        'es': "RECORDATORIO: ¡Todo el texto (intro, main_content, reflection_prompt) debe estar en ESPAÑOL!",
        'fr': "RAPPEL: Tout le texte (intro, main_content, reflection_prompt) doit être en FRANÇAIS!",
        'zh': "提醒：所有文本（intro、main_content、reflection_prompt）必须使用中文！"
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
        'es': f"Tema: {topic.get('title')}\nDescripción: {topic.get('description', '')}",
        'zh': f"主题：{topic.get('title')}\n描述：{topic.get('description', '')}"
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


def _get_feed_topic_hint(assessment_state: str) -> str:
    """Подсказка для выбора тем Ленты на основе состояния теста."""
    hints = {
        'chaos': (
            "\nСОСТОЯНИЕ УЧЕНИКА: Хаос (внимание фрагментировано, день = тушение пожаров)\n"
            "ПРИОРИТЕТ ТЕМ: внимание, привычки, маленькие системы, фокус, приоритизация\n"
        ),
        'deadlock': (
            "\nСОСТОЯНИЕ УЧЕНИКА: Тупик (стабильность без роста, «день сурка»)\n"
            "ПРИОРИТЕТ ТЕМ: навыки, deliberate practice, кейсы изменений, разрыв петли знание→бездействие\n"
        ),
        'turning_point': (
            "\nСОСТОЯНИЕ УЧЕНИКА: Поворот (всё хорошо, но хочется большего, направление неясно)\n"
            "ПРИОРИТЕТ ТЕМ: стратегия, смыслы, развитие как процесс, исследование направлений\n"
        ),
    }
    return hints.get(assessment_state, '')


def _get_feed_digest_hint(assessment_state: str) -> str:
    """Адаптация стиля дайджеста по состоянию теста."""
    hints = {
        'chaos': (
            "\nАДАПТАЦИЯ ПО СОСТОЯНИЮ (Хаос):\n"
            "- Короткие абзацы, одна мысль за раз\n"
            "- Заверши одним конкретным микро-действием: «попробуй сегодня одну вещь»\n"
            "- Не перегружай деталями\n"
        ),
        'deadlock': (
            "\nАДАПТАЦИЯ ПО СОСТОЯНИЮ (Тупик):\n"
            "- Акцент на применение, не на теорию\n"
            "- Провокационный вопрос: «ты это знаешь — почему не делаешь?»\n"
            "- Покажи пример: «вот как X изменил Y — сделай свой шаг»\n"
        ),
        'turning_point': (
            "\nАДАПТАЦИЯ ПО СОСТОЯНИЮ (Поворот):\n"
            "- Рефлексия и связи с жизненным направлением\n"
            "- Исследовательский тон: «три направления, куда можно двинуться»\n"
            "- Безопасные эксперименты вместо радикальных решений\n"
        ),
    }
    return hints.get(assessment_state, '')
