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
import asyncio

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
from i18n.prompts import (
    get_content_prompts,
    get_practice_prompts,
    get_question_prompts,
    get_feedback_prompts,
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
                async with session.post(
                    self.base_url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
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

        # Получаем контекст из MCP параллельно (guides + knowledge одновременно)
        guides_context = ""
        knowledge_context = ""

        async def fetch_guides():
            """Получаем контекст из guides MCP"""
            if not mcp_client:
                return ""
            try:
                # Запускаем все поисковые запросы параллельно
                tasks = [
                    mcp_client.semantic_search(q, lang="ru", limit=2)
                    for q in guides_search_keys[:3]
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                context_parts = []
                seen_texts = set()
                for search_results in results:
                    if isinstance(search_results, Exception):
                        continue
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
                    logger.info(f"{mcp_client.name}: найдено {len(context_parts)} фрагментов контекста")
                    return "\n\n".join(context_parts[:5])
            except Exception as e:
                logger.error(f"{mcp_client.name} search error: {e}")
            return ""

        async def fetch_knowledge():
            """Получаем контекст из knowledge MCP"""
            if not knowledge_client:
                return ""
            try:
                # Запускаем все поисковые запросы параллельно
                tasks = [
                    knowledge_client.semantic_search(q, lang="ru", limit=2, sort_by="created_at:desc")
                    for q in knowledge_search_keys[:3]
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                context_parts = []
                seen_texts = set()
                for search_results in results:
                    if isinstance(search_results, Exception):
                        continue
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
                    logger.info(f"{knowledge_client.name}: найдено {len(context_parts)} фрагментов (свежие посты)")
                    return "\n\n".join(context_parts[:5])
            except Exception as e:
                logger.error(f"{knowledge_client.name} search error: {e}")
            return ""

        # Запускаем оба MCP-запроса параллельно
        guides_context, knowledge_context = await asyncio.gather(
            fetch_guides(),
            fetch_knowledge()
        )

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

        # Получаем локализованные промпты из единого модуля
        lang = intern.get('language', 'ru')
        study_duration = intern.get('study_duration', 15)
        lp = get_content_prompts(lang, study_duration, words)

        # Определяем тип контекста для промпта
        has_both = knowledge_context and guides_context
        context_instruction = ""
        if has_both:
            context_instruction = lp['use_context']
        elif mcp_context:
            context_instruction = lp['use_context']

        system_prompt = f"""Ты — персональный наставник по системному мышлению и личному развитию.
{get_personalization_prompt(intern)}

{lp['lang_instruction']}

{lp['create_text']}
{lp['engaging']}

{lp['forbidden_header']}
{lp['forbidden_questions']}
{lp['forbidden_headers']}
{lp['forbidden_end']}
{lp['question_later']}
{context_instruction}

{ONTOLOGY_RULES}"""

        pain_point = topic.get('pain_point', '')
        key_insight = topic.get('key_insight', '')
        source = topic.get('source', '')

        user_prompt = f"""{lp['topic']}: {topic.get('title')}
{lp['main_concept']}: {topic.get('main_concept')}
{lp['related_concepts']}: {', '.join(topic.get('related_concepts', []))}

{f"{lp['pain_point']}: {pain_point}" if pain_point else ""}
{f"{lp['key_insight']}: {key_insight}" if key_insight else ""}
{f"{lp['source']}: {source}" if source else ""}

{f"{lp['content_instruction']}:{chr(10)}{content_prompt}" if content_prompt else ""}

{f"{lp['context_from']}:{chr(10)}{mcp_context}" if mcp_context else ""}

{lp['start_with']}
{lp['use_context'] if mcp_context else ""}"""""

        result = await self.generate(system_prompt, user_prompt)
        if result:
            return result
        # Локализованное сообщение об ошибке из единого модуля
        return lp.get('error_generation', "Failed to generate content.")

    async def generate_practice_intro(self, topic: dict, intern: dict) -> dict:
        """Генерирует полное описание практического задания на языке пользователя

        Args:
            topic: тема с практическим заданием
            intern: профиль стажера

        Returns:
            Dict с ключами: intro, task, work_product, examples (все на языке пользователя)
        """
        # Получаем локализованные промпты из единого модуля
        lang = intern.get('language', 'ru')
        lp = get_practice_prompts(lang)

        task_ru = topic.get('task', '')
        work_product_ru = topic.get('work_product', '')
        wp_examples = topic.get('wp_examples', [])
        wp_examples_text = "\n".join(f"• {ex}" for ex in wp_examples) if wp_examples else ""

        system_prompt = f"""Ты — персональный наставник по системному мышлению.
{get_personalization_prompt(intern)}

{lp['lang_instruction']}

{lp['intro_instruction']}
{lp['task_instruction']}
{lp['wp_instruction']}
{lp['examples_instruction']}

Выдай ответ СТРОГО в формате:
INTRO: (2-4 предложения)
TASK: (переведённое задание)
WORK_PRODUCT: (рабочий продукт)
EXAMPLES: (примеры, каждый с новой строки начиная с •)

{ONTOLOGY_RULES}"""

        user_prompt = f"""{lp['task_header']}: {topic.get('title')}
Concept: {topic.get('main_concept')}

SOURCE DATA:
Task: {task_ru}
Work product: {work_product_ru}
Examples:
{wp_examples_text}

Translate and adapt everything to the target language."""

        result = await self.generate(system_prompt, user_prompt)

        if not result:
            # Fallback: возвращаем оригинал на русском
            return {
                'intro': '',
                'task': task_ru,
                'work_product': work_product_ru,
                'examples': wp_examples_text
            }

        # Парсим ответ
        parsed = {
            'intro': '',
            'task': task_ru,
            'work_product': work_product_ru,
            'examples': wp_examples_text
        }

        try:
            lines = result.split('\n')
            current_key = None
            current_value = []

            for line in lines:
                if line.startswith('INTRO:'):
                    if current_key and current_value:
                        parsed[current_key] = '\n'.join(current_value).strip()
                    current_key = 'intro'
                    current_value = [line[6:].strip()]
                elif line.startswith('TASK:'):
                    if current_key and current_value:
                        parsed[current_key] = '\n'.join(current_value).strip()
                    current_key = 'task'
                    current_value = [line[5:].strip()]
                elif line.startswith('WORK_PRODUCT:'):
                    if current_key and current_value:
                        parsed[current_key] = '\n'.join(current_value).strip()
                    current_key = 'work_product'
                    current_value = [line[13:].strip()]
                elif line.startswith('EXAMPLES:'):
                    if current_key and current_value:
                        parsed[current_key] = '\n'.join(current_value).strip()
                    current_key = 'examples'
                    current_value = [line[9:].strip()]
                elif current_key:
                    current_value.append(line)

            # Сохраняем последний ключ
            if current_key and current_value:
                parsed[current_key] = '\n'.join(current_value).strip()

        except Exception as e:
            logger.warning(f"Error parsing practice intro response: {e}, using raw result")
            parsed['intro'] = result

        return parsed

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

        # Получаем локализованные промпты из единого модуля
        lang = intern.get('language', 'ru')
        qp = get_question_prompts(lang)

        # Определяем тип вопроса по уровню сложности
        question_type_hint = qp.get(f'question_type_{level}', qp['question_type_1'])

        # Формируем подсказки по шаблонам
        templates_hint = ""
        if question_templates:
            templates_hint = f"\n{qp['examples_hint']}\n- " + "\n- ".join(question_templates[:3])

        system_prompt = f"""Ты генерируешь ТОЛЬКО ОДИН КОРОТКИЙ ВОПРОС. Ничего больше.

{qp['lang_instruction']}

{qp['forbidden_header']}
{qp['forbidden_intro']}
{qp['forbidden_headers']}
{qp['forbidden_examples']}
{qp['forbidden_after']}

{qp['only_question']}
{qp['related_to_occupation']} "{occupation}".
{qp['complexity_level']} {bloom['short_name']} — {bloom['desc']}
{question_type_hint}
{templates_hint}

{ONTOLOGY_RULES}"""

        user_prompt = f"""{qp['topic']}: {topic.get('title')}
{qp['concept']}: {topic.get('main_concept')}

{qp['output_only_question']}"""

        result = await self.generate(system_prompt, user_prompt)
        return result or bloom['question_type'].format(concept=topic.get('main_concept', 'эту тему'))


# Создаём экземпляр клиента
claude = ClaudeClient()
