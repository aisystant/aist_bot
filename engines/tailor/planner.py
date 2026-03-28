"""
Генерация текста занятия из каталога через Claude (WP-149, SOP.001 v3).

TailorEngine.assemble() → structured lesson → generate_lesson_text() → текст для бота.

Два типа занятий:
  - worldview: компиляция мема (непродуктивный → продуктивный)
  - mastery: постановка практики (can-do × задание × assessment)
"""

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# 5 областей (PD.FORM.081)
AREA_NAMES = {
    1: "Знания",
    2: "Инструменты",
    3: "Ограничения",
    4: "Окружение",
    5: "Организм",
}


def build_lesson_prompt(lesson: dict, user_profile: dict) -> str:
    """Построить промпт для Claude из structured lesson.

    Args:
        lesson: результат TailorEngine.assemble()
        user_profile: {name, occupation, goals, interests, ...}

    Returns:
        Prompt для Claude.
    """
    area_name = lesson.get('area_name', '')
    impact_type = lesson.get('impact_type', 'worldview')
    content = lesson.get('content', {})
    retrieval = lesson.get('retrieval') or ''
    it_scaffolding = lesson.get('it_scaffolding', '')

    name = user_profile.get('name', 'пользователь')
    occupation = user_profile.get('occupation', '')
    goals = user_profile.get('goals', '')

    depth = content.get('depth', 1)

    if impact_type == 'worldview':
        return _build_worldview_prompt(
            content, area_name, depth, name, occupation, goals,
            retrieval, it_scaffolding,
        )
    else:
        return _build_mastery_prompt(
            content, area_name, depth, name, occupation, goals,
            retrieval, it_scaffolding,
        )


def _build_worldview_prompt(
    content: dict,
    area_name: str,
    depth: int,
    name: str,
    occupation: str,
    goals: str,
    retrieval: str,
    it_scaffolding: str,
) -> str:
    unproductive = content.get('meme_unproductive', '')
    productive = content.get('meme_productive', '')
    function = content.get('meme_function', '')
    compilation = content.get('compilation_method', '')
    diagnostic = content.get('diagnostic', '')
    depth_name = content.get('depth_name', '')
    depth_can_do = content.get('depth_can_do', '')
    depth_activity = content.get('depth_activity', '')
    depth_assessment = content.get('depth_assessment', '')

    # Depth-specific instructions
    if depth == 1:
        depth_instruction = f"""## Фокус: Осознание (глубина 1 из 3)
Цель: помочь человеку ЗАМЕТИТЬ у себя это убеждение. Не переубеждать, не давать антитезис.
Только диагностика и самонаблюдение.

Активность: {depth_activity or diagnostic}
Can-do: {depth_can_do or 'Может назвать убеждение и привести пример из своей жизни'}
Проверка: {depth_assessment or 'Описывает конкретный эпизод'}"""

        json_format = """{{
  "intro": "1-2 предложения: почему стоит обратить внимание на это убеждение",
  "lesson_text": "Описание убеждения: как оно звучит, как проявляется в повседневности. 150-300 слов. БЕЗ антитезиса — только узнавание.",
  "diagnostic_question": "Вопрос для самодиагностики: замечаешь ли ты у себя... (конкретный)",
  "task": "Задание на наблюдение: в течение 3 дней отслеживай, когда возникает это убеждение",
  "reflection": "Вопрос: как часто ты замечал это убеждение? В каких ситуациях?"
}}"""

    elif depth == 2:
        depth_instruction = f"""## Фокус: Различение (глубина 2 из 3)
Цель: помочь понять ПОЧЕМУ убеждение не работает и ЧТО вместо него.
Объяснить функцию мема и предложить противоречие/опыт.

Функция мема: {function}
Продуктивный антитезис: «{productive}»
Метод компиляции: {compilation}
Активность: {depth_activity or compilation}
Can-do: {depth_can_do or 'Объясняет функцию мема и формулирует антитезис'}
Проверка: {depth_assessment}"""

        json_format = """{{
  "intro": "1-2 предложения: связь с предыдущим наблюдением (глубина 1)",
  "lesson_text": "Почему убеждение держится (функция). Что не так (различение). Что вместо (антитезис). 200-400 слов.",
  "distinction_question": "Можешь объяснить: зачем это убеждение тебе нужно? Что оно защищает?",
  "contradiction_task": "Упражнение на противоречие: конкретное действие из метода компиляции",
  "reflection": "Вопрос: что изменилось в твоём понимании после упражнения?"
}}"""

    else:  # depth == 3
        depth_instruction = f"""## Фокус: Компиляция (глубина 3 из 3)
Цель: перевести в ПОВЕДЕНИЕ. Практика антитезиса в течение недели.
Не объяснять заново — человек уже знает. Дать конкретное задание.

Продуктивный антитезис: «{productive}»
Активность: {depth_activity or f'Практиковать антитезис в течение недели'}
Can-do: {depth_can_do or 'Замечает старый паттерн и выбирает новое поведение'}
Проверка: {depth_assessment or 'Описывает 2+ конкретных случая нового поведения'}"""

        json_format = """{{
  "intro": "1-2 предложения: напоминание антитезиса и переход к практике",
  "lesson_text": "Краткое (100-200 слов): как именно практиковать антитезис. Конкретные шаги на неделю.",
  "practice_task": "Недельное задание: конкретное поведенческое изменение с ежедневным чек-ином",
  "check_in": "Ежедневный вопрос для самопроверки (1 предложение)",
  "reflection": "Вопрос в конце недели: сколько раз выбрал новое поведение? Что помогло, что мешало?"
}}"""

    return f"""Ты — Портной (Tailor), роль в системе персонального развития.

## Пользователь
- Имя: {name}
- Занятие: {occupation or 'не указано'}
- Цели: {goals or 'не указаны'}

## Область: {area_name}
## Убеждение: «{unproductive}»

{depth_instruction}

{f'## Recall предыдущего занятия:{chr(10)}{retrieval}' if retrieval else ''}

## Подводка к инструментам
{it_scaffolding}

## Формат ответа

Сгенерируй занятие в формате JSON:

```json
{json_format}
```

Правила:
- Пиши на русском
- Примеры из домена пользователя ({occupation or 'универсальные примеры'})
- НЕ используй академический стиль — пиши как наставник
- Не говори «непродуктивный мем» — формулируй как «привычное убеждение»
- Включи подводку к инструментам в конце задания"""


def _build_mastery_prompt(
    content: dict,
    area_name: str,
    depth: int,
    name: str,
    occupation: str,
    goals: str,
    retrieval: str,
    it_scaffolding: str,
) -> str:
    practice_name = content.get('practice_name', '')
    degree = depth  # для практик depth = degree
    can_do = content.get('can_do', '')
    assignment = content.get('assignment', '')
    assessment = content.get('assessment', '')

    degree_names = {1: 'Объяснение', 2: 'Умение', 3: 'Навык', 4: 'Мастерство'}
    degree_name = degree_names.get(degree, 'Объяснение')

    return f"""Ты — Портной (Tailor), роль в системе персонального развития.
Задача: помочь человеку освоить практику на текущей степени мастерства.

## Пользователь
- Имя: {name}
- Занятие: {occupation or 'не указано'}
- Цели: {goals or 'не указаны'}

## Область: {area_name}
## Тип занятия: Мастерство (постановка практики)

## Практика: {practice_name}
## Степень: {degree}. {degree_name}

## Что должен уметь (can-do):
{can_do}

## Задание для перехода:
{assignment}

## Как проверить:
{assessment}

{f'## Recall предыдущего занятия:{chr(10)}{retrieval}' if retrieval else ''}

## Подводка к инструментам
{it_scaffolding}

## Формат ответа

Сгенерируй занятие в формате JSON:

```json
{{
  "intro": "1-2 предложения: зачем эта практика сейчас (персонально для пользователя)",
  "lesson_text": "Объяснение практики на текущей степени: что делать, как делать, почему именно так. 200-400 слов. Примеры из домена пользователя.",
  "check_question": "Вопрос для проверки: можешь ли ты уже... (из can-do)",
  "practice_task": "Конкретное задание на эту неделю (из задания для перехода)",
  "reflection": "Вопрос: что получается, а что пока нет?"
}}
```

Правила:
- Пиши на русском
- Примеры из домена пользователя ({occupation or 'универсальные примеры'})
- НЕ используй академический стиль — пиши как наставник
- Задание должно быть конкретным и выполнимым за неделю
- Включи подводку к инструментам в конце practice_task"""


async def generate_lesson_text(
    lesson: dict,
    user_profile: dict,
    claude_client,
    model: Optional[str] = None,
) -> Optional[dict]:
    """Сгенерировать текст занятия через Claude."""
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

        return _parse_lesson_json(response)

    except Exception as e:
        logger.warning(f"[Tailor/Planner] Claude generation failed: {e}")
        return None


def _parse_lesson_json(response: str) -> Optional[dict]:
    """Извлечь JSON из ответа Claude."""
    json_match = re.search(r'```json\s*\n?(.*?)\n?```', response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

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
    area_name = lesson.get('area_name', '')
    impact_type = lesson.get('impact_type', 'worldview')
    element_id = lesson.get('element_id', '')
    depth = lesson.get('depth', 1)

    if impact_type == 'worldview':
        depth_names = {1: 'Осознание', 2: 'Различение', 3: 'Компиляция'}
        type_label = f"Мировоззрение · {depth_names.get(depth, '')}"
    else:
        degree_names = {1: 'Объяснение', 2: 'Умение', 3: 'Навык', 4: 'Мастерство'}
        type_label = f"Мастерство · {degree_names.get(depth, '')}"

    parts = []

    # Заголовок
    parts.append(
        f"<b>{area_name}</b> · {type_label}\n"
        f"<i>{element_id}</i>\n"
    )

    # Intro
    intro = generated.get('intro', '')
    if intro:
        parts.append(f"{intro}\n")

    # Основной текст
    text = generated.get('lesson_text', '')
    if text:
        parts.append(f"{text}\n")

    # Вопрос (varies by depth and type)
    question = (
        generated.get('diagnostic_question')
        or generated.get('distinction_question')
        or generated.get('check_question')
        or generated.get('question', '')
    )
    if question:
        parts.append(f"\n<b>Вопрос:</b>\n{question}\n")

    # Задание (varies by depth and type)
    task = (
        generated.get('compilation_task')
        or generated.get('contradiction_task')
        or generated.get('practice_task')
        or generated.get('task', '')
    )
    if task:
        parts.append(f"\n<b>Задание:</b>\n{task}\n")

    # Check-in (depth 3 worldview)
    check_in = generated.get('check_in', '')
    if check_in:
        parts.append(f"\n<b>Ежедневный чек-ин:</b>\n{check_in}\n")

    # Рефлексия
    reflection = generated.get('reflection', '')
    if reflection:
        parts.append(f"\n<b>Рефлексия:</b>\n{reflection}")

    return "\n".join(parts)
