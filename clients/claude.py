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

        # Определяем язык ответа и локализуем промпт
        lang = intern.get('language', 'ru')

        # Локализованные инструкции для разных языков
        LANG_PROMPTS = {
            'ru': {
                'lang_instruction': "ВАЖНО: Пиши ВСЁ на русском языке.",
                'create_text': f"Создай текст на {intern['study_duration']} минут чтения (~{words} слов). Без заголовков, только абзацы.",
                'engaging': "Текст должен быть вовлекающим, с примерами из жизни читателя.",
                'forbidden_header': "СТРОГО ЗАПРЕЩЕНО:",
                'forbidden_questions': "- Добавлять вопросы в любом месте текста",
                'forbidden_headers': "- Использовать заголовки типа \"Вопрос:\", \"Вопрос для размышления:\" и т.п.",
                'forbidden_end': "- Заканчивать текст вопросом",
                'question_later': "Вопрос будет задан отдельно после текста.",
                'topic': "Тема",
                'main_concept': "Основное понятие",
                'related_concepts': "Связанные понятия",
                'pain_point': "Боль читателя",
                'key_insight': "Ключевой инсайт",
                'source': "Источник",
                'content_instruction': "ИНСТРУКЦИЯ ПО КОНТЕНТУ",
                'context_from': "КОНТЕКСТ ИЗ МАТЕРИАЛОВ AISYSTANT",
                'start_with': "Начни с признания боли читателя, затем раскрой тему и подведи к ключевому инсайту.",
                'use_context': "Опирайся на контекст, но адаптируй под профиль стажера. Актуальные посты важнее."
            },
            'en': {
                'lang_instruction': "IMPORTANT: Write EVERYTHING in English.",
                'create_text': f"Create a text for {intern['study_duration']} minutes of reading (~{words} words). No headings, only paragraphs.",
                'engaging': "The text should be engaging, with examples from the reader's life.",
                'forbidden_header': "STRICTLY FORBIDDEN:",
                'forbidden_questions': "- Adding questions anywhere in the text",
                'forbidden_headers': "- Using headers like \"Question:\", \"Question for reflection:\" etc.",
                'forbidden_end': "- Ending the text with a question",
                'question_later': "A question will be asked separately after the text.",
                'topic': "Topic",
                'main_concept': "Main concept",
                'related_concepts': "Related concepts",
                'pain_point': "Reader's pain",
                'key_insight': "Key insight",
                'source': "Source",
                'content_instruction': "CONTENT INSTRUCTION",
                'context_from': "CONTEXT FROM AISYSTANT MATERIALS",
                'start_with': "Start by acknowledging the reader's pain, then develop the topic and lead to the key insight.",
                'use_context': "Use the context, but adapt it to the student's profile. Recent posts are more important."
            },
            'es': {
                'lang_instruction': "IMPORTANTE: Escribe TODO en español.",
                'create_text': f"Crea un texto para {intern['study_duration']} minutos de lectura (~{words} palabras). Sin títulos, solo párrafos.",
                'engaging': "El texto debe ser atractivo, con ejemplos de la vida del lector.",
                'forbidden_header': "ESTRICTAMENTE PROHIBIDO:",
                'forbidden_questions': "- Agregar preguntas en cualquier parte del texto",
                'forbidden_headers': "- Usar encabezados como \"Pregunta:\", \"Pregunta para reflexionar:\" etc.",
                'forbidden_end': "- Terminar el texto con una pregunta",
                'question_later': "Se hará una pregunta por separado después del texto.",
                'topic': "Tema",
                'main_concept': "Concepto principal",
                'related_concepts': "Conceptos relacionados",
                'pain_point': "Dolor del lector",
                'key_insight': "Idea clave",
                'source': "Fuente",
                'content_instruction': "INSTRUCCIÓN DE CONTENIDO",
                'context_from': "CONTEXTO DE MATERIALES AISYSTANT",
                'start_with': "Comienza reconociendo el dolor del lector, luego desarrolla el tema y lleva a la idea clave.",
                'use_context': "Usa el contexto, pero adáptalo al perfil del estudiante. Las publicaciones recientes son más importantes."
            },
            'fr': {
                'lang_instruction': "IMPORTANT: Écris TOUT en français.",
                'create_text': f"Crée un texte pour {intern['study_duration']} minutes de lecture (~{words} mots). Sans titres, seulement des paragraphes.",
                'engaging': "Le texte doit être engageant, avec des exemples de la vie du lecteur.",
                'forbidden_header': "STRICTEMENT INTERDIT:",
                'forbidden_questions': "- Ajouter des questions n'importe où dans le texte",
                'forbidden_headers': "- Utiliser des en-têtes comme \"Question:\", \"Question de réflexion:\" etc.",
                'forbidden_end': "- Terminer le texte par une question",
                'question_later': "Une question sera posée séparément après le texte.",
                'topic': "Sujet",
                'main_concept': "Concept principal",
                'related_concepts': "Concepts liés",
                'pain_point': "Douleur du lecteur",
                'key_insight': "Idée clé",
                'source': "Source",
                'content_instruction': "INSTRUCTION DE CONTENU",
                'context_from': "CONTEXTE DES MATÉRIAUX AISYSTANT",
                'start_with': "Commence par reconnaître la douleur du lecteur, puis développe le sujet et mène à l'idée clé.",
                'use_context': "Utilise le contexte, mais adapte-le au profil de l'étudiant. Les publications récentes sont plus importantes."
            }
        }

        lp = LANG_PROMPTS.get(lang, LANG_PROMPTS['en'])

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
        # Локализованное сообщение об ошибке
        error_messages = {
            'ru': "Не удалось сгенерировать контент. Попробуйте /learn ещё раз.",
            'en': "Failed to generate content. Please try /learn again.",
            'es': "No se pudo generar el contenido. Por favor, intente /learn de nuevo.",
            'fr': "Échec de la génération du contenu. Veuillez réessayer /learn."
        }
        return error_messages.get(lang, error_messages['en'])

    async def generate_practice_intro(self, topic: dict, intern: dict) -> dict:
        """Генерирует полное описание практического задания на языке пользователя

        Args:
            topic: тема с практическим заданием
            intern: профиль стажера

        Returns:
            Dict с ключами: intro, task, work_product, examples (все на языке пользователя)
        """
        # Определяем язык ответа
        lang = intern.get('language', 'ru')
        lang_instruction = {
            'ru': "ВАЖНО: Пиши ВСЁ на русском языке.",
            'en': "IMPORTANT: Write EVERYTHING in English.",
            'es': "IMPORTANTE: Escribe TODO en español.",
            'fr': "IMPORTANT: Écris TOUT en français."
        }.get(lang, "IMPORTANT: Write EVERYTHING in English.")

        task_ru = topic.get('task', '')
        work_product_ru = topic.get('work_product', '')
        wp_examples = topic.get('wp_examples', [])
        wp_examples_text = "\n".join(f"• {ex}" for ex in wp_examples) if wp_examples else ""

        system_prompt = f"""Ты — персональный наставник по системному мышлению.
{get_personalization_prompt(intern)}

{lang_instruction}

Твоя задача — подготовить полное описание практического задания.
Ты ДОЛЖЕН перевести/адаптировать ВСЕ части на целевой язык.

Выдай ответ СТРОГО в формате:
INTRO: (2-4 предложения, зачем это задание)
TASK: (переведённое задание)
WORK_PRODUCT: (переведённый рабочий продукт)
EXAMPLES: (переведённые примеры, каждый с новой строки начиная с •)

{ONTOLOGY_RULES}"""

        user_prompt = f"""Тема: {topic.get('title')}
Понятие: {topic.get('main_concept')}

ИСХОДНЫЕ ДАННЫЕ (переведи на целевой язык):
Задание: {task_ru}
Рабочий продукт: {work_product_ru}
Примеры РП:
{wp_examples_text}

Переведи и адаптируй всё на целевой язык."""

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
            'ru': "ВАЖНО: Задай вопрос на русском языке.",
            'en': "IMPORTANT: Ask the question in English.",
            'es': "IMPORTANTE: Haz la pregunta en español.",
            'fr': "IMPORTANT: Pose la question en français."
        }.get(lang, "IMPORTANT: Ask the question in English.")

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
