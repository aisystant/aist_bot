"""
Генерация текста занятия из ячейки через Claude (WP-149).

TailorEngine.assemble() → structured lesson → generate_lesson_text() → текст для бота.

MVP: Haiku для генерации текста (быстро, дёшево).
Ф2: Sonnet для сложных адаптаций.
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Направления (для промпта)
DIRECTION_NAMES = {
    1: "Картины мира и мировоззрение",
    2: "Навыки и мастерство",
    3: "Ограничения и состояния",
    4: "Экзокортекс и ИИ",
    5: "Окружение и культура",
    6: "Организм и личные ресурсы",
}

BLOOM_NAMES = {
    1: "Запоминание",
    2: "Понимание",
    3: "Применение",
    4: "Анализ",
    5: "Создание",
}


def build_lesson_prompt(lesson: dict, user_profile: dict) -> str:
    """Построить промпт для Claude из structured lesson.

    Args:
        lesson: результат TailorEngine.assemble()
        user_profile: {name, occupation, goals, interests, ...}

    Returns:
        System + user prompt для Claude.
    """
    topic_name = lesson.get('topic_name', '')
    direction_name = DIRECTION_NAMES.get(lesson['direction'], '')
    bloom = lesson.get('bloom_level', 'Remember')
    bloom_depth = lesson.get('bloom_depth', 1)
    bloom_desc = BLOOM_NAMES.get(bloom_depth, 'Запоминание')

    can_do = lesson.get('can_do', [])
    can_do_text = "\n".join(f"  - {cd}" for cd in can_do)

    methods_notes = lesson.get('methods_notes', '')
    form = lesson.get('form', '')
    common_errors = lesson.get('common_errors', [])
    errors_text = "\n".join(f"  - {e}" for e in common_errors)

    assessment = lesson.get('assessment', {})
    bridge = lesson.get('bridge') or ''
    retrieval = lesson.get('retrieval') or ''
    it_scaffolding = lesson.get('it_scaffolding', '')

    name = user_profile.get('name', 'пользователь')
    occupation = user_profile.get('occupation', '')
    goals = user_profile.get('goals', '')

    return f"""Ты — Портной (Tailor), роль в системе персонального развития.
Твоя задача: собрать персональное занятие для конкретного человека.

## Пользователь
- Имя: {name}
- Занятие: {occupation or 'не указано'}
- Цели: {goals or 'не указаны'}

## Параметры занятия
- Тема: {topic_name}
- Направление: {direction_name}
- Глубина: {bloom_desc} (уровень {bloom_depth})

## Что пользователь должен уметь после занятия (can-do):
{can_do_text}

## Методические указания:
{methods_notes}

## Типичные ошибки (предупреди или объясни):
{errors_text}

## Форма подачи:
{form}

{'## Связь с предыдущим (bridge):' + chr(10) + bridge if bridge else ''}
{'## Recall предыдущей темы:' + chr(10) + retrieval if retrieval else ''}
## Подводка к инструментам:
{it_scaffolding}

## Формат ответа

Сгенерируй занятие в формате JSON:

```json
{{
  "intro": "1-2 предложения: зачем эта тема сегодня (персонально для пользователя)",
  "lesson_text": "Основной текст занятия (200-500 слов). Доступный язык. Примеры из домена пользователя.",
  "question": "Открытый вопрос для проверки понимания (из can-do)",
  "task": "Практическое задание (из can-do или assessment)",
  "reflection": "Вопрос для рефлексии: что изменилось в понимании?"
}}
```

Правила:
- Пиши на русском
- Примеры из домена пользователя (занятие: {occupation or 'универсальные примеры'})
- НЕ используй академический стиль — пиши как наставник
- Если bridge есть — начни intro с него
- Включи подводку к инструментам в конце task"""


async def generate_lesson_text(
    lesson: dict,
    user_profile: dict,
    claude_client,
    model: Optional[str] = None,
) -> Optional[dict]:
    """Сгенерировать текст занятия через Claude.

    Args:
        lesson: structured lesson из TailorEngine.assemble()
        user_profile: профиль пользователя
        claude_client: ClaudeClient из clients/
        model: модель Claude (default: Haiku для MVP)

    Returns:
        {intro, lesson_text, question, task, reflection} или None
    """
    try:
        from config import CLAUDE_MODEL_HAIKU
        model = model or CLAUDE_MODEL_HAIKU
    except ImportError:
        model = model or 'claude-haiku-4-5-20251001'

    prompt = build_lesson_prompt(lesson, user_profile)

    try:
        response = await claude_client.generate(
            prompt=prompt,
            max_tokens=2048,
            model=model,
        )

        if not response:
            logger.warning("[Tailor/Planner] Empty Claude response")
            return None

        # Парсинг JSON из ответа
        return _parse_lesson_json(response)

    except Exception as e:
        logger.warning(f"[Tailor/Planner] Claude generation failed: {e}")
        return None


def _parse_lesson_json(response: str) -> Optional[dict]:
    """Извлечь JSON из ответа Claude."""
    # Попробовать найти JSON-блок
    import re
    json_match = re.search(r'```json\s*\n?(.*?)\n?```', response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Попробовать весь ответ как JSON
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    # Fallback: вернуть как plain text
    logger.warning("[Tailor/Planner] Could not parse JSON, using raw text")
    return {
        'intro': '',
        'lesson_text': response[:2000],
        'question': '',
        'task': '',
        'reflection': '',
    }


def format_lesson_for_bot(
    lesson: dict,
    generated: dict,
    lang: str = 'ru',
) -> str:
    """Форматировать занятие для отправки в Telegram (HTML).

    Args:
        lesson: structured lesson
        generated: результат generate_lesson_text()
        lang: язык

    Returns:
        HTML-текст для Telegram.
    """
    topic_name = lesson.get('topic_name', '')
    direction = DIRECTION_NAMES.get(lesson.get('direction', 0), '')
    bloom_depth = lesson.get('bloom_depth', 1)

    intro = generated.get('intro', '')
    text = generated.get('lesson_text', '')
    question = generated.get('question', '')
    task = generated.get('task', '')
    reflection = generated.get('reflection', '')

    parts = []

    # Заголовок
    parts.append(
        f"<b>{topic_name}</b>\n"
        f"<i>{direction} · Глубина {bloom_depth}</i>\n"
    )

    # Intro
    if intro:
        parts.append(f"{intro}\n")

    # Основной текст
    if text:
        parts.append(f"{text}\n")

    # Вопрос
    if question:
        parts.append(f"\n❓ <b>Вопрос:</b>\n{question}\n")

    # Задание
    if task:
        parts.append(f"\n📝 <b>Задание:</b>\n{task}\n")

    # Рефлексия
    if reflection:
        parts.append(f"\n💭 <b>Рефлексия:</b>\n{reflection}")

    return "\n".join(parts)
