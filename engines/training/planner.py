"""
Генерация заданий и AI-оценка ответов для режима Тренировка.

Две ключевые функции:
- generate_assignment_text() — персонализирует задание из ячейки
- evaluate_training_answer() — оценивает ответ по criteria + common_errors
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

from clients import claude
from config import (
    get_logger,
    BASE_DIR,
    CLAUDE_MODEL_HAIKU,
    TRAINING_PASS_THRESHOLD,
    TRAINING_PARTIAL_THRESHOLD,
)

logger = get_logger(__name__)

BLOOM_LABELS = {
    'Remember': 'Запоминание',
    'Understand': 'Понимание',
    'Apply': 'Применение',
    'Analyze': 'Анализ',
    'Create': 'Создание',
}

# Fallback-имена принципов (если JSON не загрузится)
ZP_PRINCIPLE_NAMES = {
    'ZP.1': 'Аксиоматичность',
    'ZP.2': 'Структура',
    'ZP.3': 'Многомасштабность',
    'ZP.4': 'Оптимизация',
    'ZP.5': 'Бесконечное развитие',
    'ZP.6': 'Научный метод',
}


def load_zp_cells() -> dict:
    """Загрузить данные ZP-ячеек из JSON.

    НЕ кэшируется через lru_cache — при ошибке загрузки
    закешируется пустой dict навсегда. Вместо этого: module-level cache
    с возможностью повторной попытки.
    """
    global _zp_cells_cache
    if _zp_cells_cache is not None:
        return _zp_cells_cache

    path = BASE_DIR / "data" / "curriculum" / "zp_cells.json"
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            logger.info(f"Loaded {len(data)} ZP cells from {path}")
            _zp_cells_cache = data
            return data
    except FileNotFoundError:
        logger.error(f"ZP cells file not found: {path} (BASE_DIR={BASE_DIR})")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"ZP cells JSON error: {e}")
        return {}


_zp_cells_cache: dict = None


def get_principle_name(principle_id: str) -> str:
    """Получить имя принципа (из JSON или fallback)."""
    cells = load_zp_cells()
    name = cells.get(principle_id, {}).get('name')
    if name:
        return name
    return ZP_PRINCIPLE_NAMES.get(principle_id, principle_id)


async def generate_assignment_text(
    cell_data: dict,
    cognitive_level: str,
    intern: Optional[dict],
    principle_name: str,
    depth: int,
) -> str:
    """Сгенерировать персонализированный текст задания из данных ячейки.

    Args:
        cell_data: данные ячейки (одна глубина)
        cognitive_level: ключ когнитивного уровня (postformal, formal_operational, etc.)
        intern: профиль пользователя
        principle_name: название принципа
        depth: номер глубины (1-5)

    Returns:
        Текст задания для пользователя
    """
    # Получить форму для когнитивного уровня
    forms = cell_data.get('forms', {})
    form_text = forms.get(cognitive_level, forms.get('postformal', ''))

    transfer_test = cell_data.get('transfer_test', '')
    can_do = cell_data.get('can_do', [])
    domains = cell_data.get('domains', [])
    bloom_level = cell_data.get('bloom_level', '')
    bloom_label = BLOOM_LABELS.get(bloom_level, bloom_level)

    # Профиль пользователя
    name = ''
    occupation = ''
    interests = ''
    if intern:
        name = intern.get('name', '')
        occupation = intern.get('occupation', '')
        raw_interests = intern.get('interests', '')
        if isinstance(raw_interests, list):
            interests = ', '.join(raw_interests)
        elif isinstance(raw_interests, str):
            try:
                parsed = json.loads(raw_interests)
                interests = ', '.join(parsed) if isinstance(parsed, list) else raw_interests
            except (json.JSONDecodeError, TypeError):
                interests = raw_interests

    system_prompt = f"""Ты тренер мышления. Создай задание для тренировки принципа "{principle_name}" на глубине {depth} ({bloom_label}).

ПРАВИЛА:
- Задание должно быть конкретным и выполнимым в текстовом ответе
- Используй домены из списка для контекста задания: {', '.join(domains)}
- Адаптируй под занятие/интересы пользователя если указаны
- НЕ давай ответ на задание
- НЕ используй Markdown-форматирование с ** или __ (Telegram его ломает)
- Формулируй на русском языке
- Длина: 3-7 предложений

ЦЕЛЕВЫЕ НАВЫКИ (can_do):
{chr(10).join(f'- {c}' for c in can_do)}

ШАБЛОН ЗАДАНИЯ (transfer_test):
{transfer_test}

ФОРМА ДЛЯ КОГНИТИВНОГО УРОВНЯ:
{form_text}"""

    user_prompt = f"Создай задание для: {name or 'пользователь'}"
    if occupation:
        user_prompt += f", занятие: {occupation}"
    if interests:
        user_prompt += f", интересы: {interests}"

    response = await claude.generate(
        system_prompt, user_prompt,
        max_tokens=500, model=CLAUDE_MODEL_HAIKU,
    )

    if not response:
        # Fallback: вернуть transfer_test напрямую
        return transfer_test or f"Выполните задание по принципу {principle_name} (глубина {depth})."

    return response


async def evaluate_training_answer(
    answer_text: str,
    cell_data: dict,
    assignment_text: str,
    intern: Optional[dict],
) -> dict:
    """Оценить ответ пользователя через AI.

    Args:
        answer_text: текст ответа пользователя
        cell_data: данные ячейки
        assignment_text: текст задания
        intern: профиль пользователя

    Returns:
        {passed: bool, partial: bool, feedback: str}
    """
    criteria = cell_data.get('criteria', '')
    common_errors = cell_data.get('common_errors', [])
    can_do = cell_data.get('can_do', [])

    errors_text = '\n'.join(
        f"- {e.get('error', '')}: {e.get('why', '')}" for e in common_errors
    )
    can_do_text = '\n'.join(f"- {c}" for c in can_do)

    system_prompt = f"""Ты оцениваешь ответ на задание по тренировке мышления.

ЗАДАНИЕ БЫЛО:
{assignment_text}

КРИТЕРИИ ОЦЕНКИ:
{criteria}

ЦЕЛЕВЫЕ НАВЫКИ:
{can_do_text}

ТИПИЧНЫЕ ОШИБКИ (проверь, не допустил ли ученик):
{errors_text}

ОЦЕНИ ОТВЕТ и верни JSON:
{{
  "score": <число от 0.0 до 1.0>,
  "passed": <true если score >= {TRAINING_PASS_THRESHOLD}>,
  "partial": <true если score >= {TRAINING_PARTIAL_THRESHOLD} и score < {TRAINING_PASS_THRESHOLD}>,
  "feedback": "<конструктивная обратная связь на русском, 2-4 предложения>"
}}

ПРАВИЛА:
- score >= {TRAINING_PASS_THRESHOLD}: PASSED — ученик демонстрирует навыки из can_do
- score >= {TRAINING_PARTIAL_THRESHOLD}: PARTIAL — частично верно, нужна доработка
- score < {TRAINING_PARTIAL_THRESHOLD}: FAIL — фундаментальное непонимание
- В feedback объясни ЧТО хорошо и ЧТО доработать
- Если обнаружил типичную ошибку — укажи её мягко
- Верни ТОЛЬКО JSON, без лишнего текста"""

    response = await claude.generate(
        system_prompt, f"ОТВЕТ УЧЕНИКА:\n{answer_text}",
        max_tokens=500, model=CLAUDE_MODEL_HAIKU,
    )

    if not response:
        return {'passed': False, 'partial': False, 'feedback': 'Не удалось оценить ответ. Попробуйте ещё раз.'}

    # Parse JSON response
    try:
        # Извлечь JSON из ответа (может быть обёрнут в markdown)
        json_text = response.strip()
        if json_text.startswith('```'):
            json_text = json_text.split('\n', 1)[1] if '\n' in json_text else json_text
            json_text = json_text.rsplit('```', 1)[0]
        result = json.loads(json_text)
        return {
            'passed': bool(result.get('passed', False)),
            'partial': bool(result.get('partial', False)),
            'feedback': str(result.get('feedback', '')),
        }
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"Failed to parse evaluation JSON: {e}, response: {response[:200]}")
        # Fallback: treat as feedback text
        return {
            'passed': False,
            'partial': True,
            'feedback': response[:500],
        }
