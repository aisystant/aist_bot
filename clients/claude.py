"""
Клиент для работы с Claude API.

ClaudeClient - асинхронный клиент для генерации контента через Claude API.
Поддерживает:
- Генерацию теоретического контента с персонализацией
- Генерацию вопросов по уровням сложности (Блум)
- Генерацию введений к практическим заданиям
- Интеграцию с MCP для получения контекста
"""

from typing import Optional

import aiohttp

from config import (
    get_logger,
    ANTHROPIC_API_KEY,
    STUDY_DURATIONS,
    BLOOM_LEVELS,
    COMPLEXITY_LEVELS,
    ONTOLOGY_RULES,
)
from core.helpers import (
    get_personalization_prompt,
    load_topic_metadata,
    get_search_keys,
    get_bloom_questions,
)

logger = get_logger(__name__)


class ClaudeClient:
    """Клиент для работы с Claude API"""

    def __init__(self):
        self.api_key = ANTHROPIC_API_KEY
        self.base_url = "https://api.anthropic.com/v1/messages"

    async def generate(self, system_prompt: str, user_prompt: str) -> Optional[str]:
        """Базовый метод генерации текста через Claude API

        Args:
            system_prompt: системный промпт
            user_prompt: пользовательский промпт

        Returns:
            Сгенерированный текст или None при ошибке
        """
        async with aiohttp.ClientSession() as session:
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01"
            }

            payload = {
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}]
            }

            try:
                async with session.post(self.base_url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["content"][0]["text"]
                    else:
                        error = await resp.text()
                        logger.error(f"Claude API error: {error}")
                        return None
            except Exception as e:
                logger.error(f"Claude API exception: {e}")
                return None

    async def generate_content(self, topic: dict, intern: dict, mcp_client=None, knowledge_client=None) -> str:
        """Генерирует контент для теоретической темы марафона

        Args:
            topic: тема для генерации
            intern: профиль стажера
            mcp_client: клиент MCP для руководств (guides)
            knowledge_client: клиент MCP для базы знаний (knowledge) - приоритет свежим постам

        Returns:
            Сгенерированный контент или сообщение об ошибке
        """
        duration = STUDY_DURATIONS.get(str(intern['study_duration']), {"words": 1500})
        words = duration.get('words', 1500)

        # Пробуем загрузить метаданные темы для точных поисковых запросов
        topic_id = topic.get('id', '')
        metadata = load_topic_metadata(topic_id) if topic_id else None

        # Используем ключи поиска из метаданных или формируем общий запрос
        if metadata:
            guides_search_keys = get_search_keys(metadata, "guides_mcp")
            knowledge_search_keys = get_search_keys(metadata, "knowledge_mcp")
            logger.info(f"Загружены метаданные темы {topic_id}: {len(guides_search_keys)} guides, {len(knowledge_search_keys)} knowledge")
        else:
            # Fallback на общий запрос
            default_query = f"{topic.get('title')} {topic.get('main_concept')}"
            guides_search_keys = [default_query]
            knowledge_search_keys = [default_query]

        # Получаем контекст из MCP руководств (используем все ключи поиска)
        guides_context = ""
        if mcp_client:
            try:
                context_parts = []
                seen_texts = set()  # Для дедупликации
                for search_query in guides_search_keys[:3]:  # Максимум 3 запроса
                    search_results = await mcp_client.semantic_search(search_query, lang="ru", limit=2)
                    if search_results:
                        for item in search_results:
                            if isinstance(item, dict):
                                text = item.get('text', item.get('content', ''))
                            elif isinstance(item, str):
                                text = item
                            else:
                                continue
                            if text and text[:100] not in seen_texts:
                                seen_texts.add(text[:100])
                                context_parts.append(text[:1500])
                if context_parts:
                    guides_context = "\n\n".join(context_parts[:5])  # Максимум 5 фрагментов
                    logger.info(f"{mcp_client.name}: найдено {len(context_parts)} фрагментов контекста")
            except Exception as e:
                logger.error(f"{mcp_client.name} search error: {e}")

        # Получаем контекст из MCP базы знаний (knowledge MCP использует инструмент 'search')
        knowledge_context = ""
        if knowledge_client:
            try:
                context_parts = []
                seen_texts = set()
                for search_query in knowledge_search_keys[:3]:  # Максимум 3 запроса
                    # Сортируем по дате создания (сначала новые)
                    search_results = await knowledge_client.semantic_search(
                        search_query, lang="ru", limit=2, sort_by="created_at:desc"
                    )
                    if search_results:
                        for item in search_results:
                            if isinstance(item, dict):
                                text = item.get('text', item.get('content', ''))
                                date_info = item.get('created_at', item.get('date', ''))
                                if date_info:
                                    text = f"[{date_info}] {text}"
                            elif isinstance(item, str):
                                text = item
                            else:
                                continue
                            if text and text[:100] not in seen_texts:
                                seen_texts.add(text[:100])
                                context_parts.append(text[:1500])
                if context_parts:
                    knowledge_context = "\n\n".join(context_parts[:5])  # Максимум 5 фрагментов
                    logger.info(f"{knowledge_client.name}: найдено {len(context_parts)} фрагментов (свежие посты)")
            except Exception as e:
                logger.error(f"{knowledge_client.name} search error: {e}")

        # Объединяем контексты (knowledge имеет приоритет, поэтому идёт первым)
        mcp_context = ""
        if knowledge_context and guides_context:
            mcp_context = f"АКТУАЛЬНЫЕ ПОСТЫ:\n{knowledge_context}\n\n---\n\nИЗ РУКОВОДСТВ:\n{guides_context}"
        elif knowledge_context:
            mcp_context = knowledge_context
        elif guides_context:
            mcp_context = guides_context

        # Используем content_prompt из структуры знаний, если есть
        content_prompt = topic.get('content_prompt', '')

        # Определяем тип контекста для промпта
        has_both = knowledge_context and guides_context
        context_instruction = ""
        if has_both:
            context_instruction = "Используй предоставленный контекст: актуальные посты имеют приоритет, руководства дополняют."
        elif mcp_context:
            context_instruction = "Используй предоставленный контекст из материалов Aisystant как основу."

        # Определяем язык ответа
        lang = intern.get('language', 'ru')
        lang_instruction = {
            'ru': "Пиши на русском языке.",
            'en': "Write in English.",
            'es': "Escribe en español."
        }.get(lang, "Пиши на русском языке.")

        system_prompt = f"""Ты — персональный наставник по системному мышлению и личному развитию.
{get_personalization_prompt(intern)}

{lang_instruction}

Создай текст на {intern['study_duration']} минут чтения (~{words} слов). Без заголовков, только абзацы.
Текст должен быть вовлекающим, с примерами из жизни читателя.

СТРОГО ЗАПРЕЩЕНО:
- Добавлять вопросы в любом месте текста
- Использовать заголовки типа "Вопрос:", "Вопрос для размышления:", "Вопрос для проверки:" и т.п.
- Заканчивать текст вопросом
Вопрос будет задан отдельно после текста.
{context_instruction}

{ONTOLOGY_RULES}"""

        pain_point = topic.get('pain_point', '')
        key_insight = topic.get('key_insight', '')
        source = topic.get('source', '')

        user_prompt = f"""Тема: {topic.get('title')}
Основное понятие: {topic.get('main_concept')}
Связанные понятия: {', '.join(topic.get('related_concepts', []))}

{"Боль читателя: " + pain_point if pain_point else ""}
{"Ключевой инсайт: " + key_insight if key_insight else ""}
{"Источник: " + source if source else ""}

{f"ИНСТРУКЦИЯ ПО КОНТЕНТУ:{chr(10)}{content_prompt}" if content_prompt else ""}

{f"КОНТЕКСТ ИЗ МАТЕРИАЛОВ AISYSTANT:{chr(10)}{mcp_context}" if mcp_context else ""}

Начни с признания боли читателя, затем раскрой тему и подведи к ключевому инсайту.
{"Опирайся на контекст, но адаптируй под профиль стажера. Актуальные посты важнее." if mcp_context else ""}"""

        result = await self.generate(system_prompt, user_prompt)
        return result or "Не удалось сгенерировать контент. Попробуйте /learn ещё раз."

    async def generate_practice_intro(self, topic: dict, intern: dict) -> str:
        """Генерирует вводный текст для практического задания

        Args:
            topic: тема с практическим заданием
            intern: профиль стажера

        Returns:
            Вводный текст или пустая строка при ошибке
        """
        # Определяем язык ответа
        lang = intern.get('language', 'ru')
        lang_instruction = {
            'ru': "Пиши на русском языке.",
            'en': "Write in English.",
            'es': "Escribe en español."
        }.get(lang, "Пиши на русском языке.")

        system_prompt = f"""Ты — персональный наставник по системному мышлению.
{get_personalization_prompt(intern)}

{lang_instruction}

Напиши краткое (3-5 предложений) введение к практическому заданию.
Объясни, зачем это задание и как оно связано с темой дня.

{ONTOLOGY_RULES}"""

        task = topic.get('task', '')
        work_product = topic.get('work_product', '')

        user_prompt = f"""Практическое задание: {topic.get('title')}
Основное понятие: {topic.get('main_concept')}

Задание: {task}
Рабочий продукт: {work_product}

Напиши краткое введение, которое мотивирует выполнить задание."""

        result = await self.generate(system_prompt, user_prompt)
        return result or ""

    async def generate_question(self, topic: dict, intern: dict, bloom_level: int = None) -> str:
        """Генерирует вопрос по теме с учётом уровня сложности и метаданных темы

        Использует шаблоны вопросов из метаданных темы (topics/*.yaml) если доступны.
        Учитывает:
        - Сложность 1 (Различения): вопросы "в чём разница"
        - Сложность 2 (Понимание): открытые вопросы
        - Сложность 3 (Применение): анализ, примеры из жизни/работы

        Args:
            topic: тема для вопроса
            intern: профиль стажера
            bloom_level: уровень сложности (1, 2 или 3)

        Returns:
            Сгенерированный вопрос
        """
        # Используем bloom_level для обратной совместимости, но теперь это "сложность"
        level = bloom_level or intern.get('bloom_level', intern.get('complexity_level', 1))
        bloom = BLOOM_LEVELS.get(level, BLOOM_LEVELS[1])
        occupation = intern.get('occupation', '') or 'работа'
        study_duration = intern.get('study_duration', 15)

        # Пробуем загрузить метаданные темы
        topic_id = topic.get('id', '')
        metadata = load_topic_metadata(topic_id) if topic_id else None

        # Получаем настройки вопросов из метаданных
        question_config = {}
        question_templates = []
        if metadata:
            question_config = get_bloom_questions(metadata, level, study_duration)
            question_templates = question_config.get('question_templates', [])
            logger.info(f"Загружены шаблоны вопросов для {topic_id}: bloom_{level}, {study_duration}мин, {len(question_templates)} шаблонов")

        # Определяем тип вопроса по уровню сложности
        question_type_hints = {
            1: "Задай вопрос на РАЗЛИЧЕНИЕ понятий (\"В чём разница между...\", \"Чем отличается...\").",
            2: "Задай ОТКРЫТЫЙ вопрос на понимание (\"Почему...\", \"Как вы понимаете...\", \"Объясните связь...\").",
            3: "Задай вопрос на ПРИМЕНЕНИЕ и АНАЛИЗ (\"Приведите пример из жизни\", \"Проанализируйте ситуацию\", \"Как бы вы объяснили коллеге...\")."
        }
        question_type_hint = question_type_hints.get(level, question_type_hints[1])

        # Формируем подсказки по шаблонам
        templates_hint = ""
        if question_templates:
            templates_hint = f"\nПРИМЕРЫ ВОПРОСОВ (используй как образец стиля):\n- " + "\n- ".join(question_templates[:3])

        # Определяем язык ответа
        lang = intern.get('language', 'ru')
        lang_instruction = {
            'ru': "Задай вопрос на русском языке.",
            'en': "Ask the question in English.",
            'es': "Haz la pregunta en español."
        }.get(lang, "Задай вопрос на русском языке.")

        system_prompt = f"""Ты генерируешь ТОЛЬКО ОДИН КОРОТКИЙ ВОПРОС. Ничего больше.

{lang_instruction}

СТРОГО ЗАПРЕЩЕНО:
- Писать введение, объяснения, контекст или любой текст перед вопросом
- Писать заголовки типа "Вопрос:", "Вопрос для размышления:" и т.п.
- Писать примеры, истории, мотивацию
- Писать что-либо после вопроса

Выдай ТОЛЬКО сам вопрос — 1-3 предложения максимум.
Вопрос должен быть связан с профессией: "{occupation}".
Уровень сложности: {bloom['short_name']} — {bloom['desc']}
{question_type_hint}
{templates_hint}

{ONTOLOGY_RULES}"""

        user_prompt = f"""Тема: {topic.get('title')}
Понятие: {topic.get('main_concept')}

Выдай ТОЛЬКО вопрос (1-3 предложения), без введения и пояснений."""

        result = await self.generate(system_prompt, user_prompt)
        return result or bloom['question_type'].format(concept=topic.get('main_concept', 'эту тему'))


# Создаём экземпляр клиента
claude = ClaudeClient()
