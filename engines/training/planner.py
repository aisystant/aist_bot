"""
Генерация заданий и AI-оценка ответов для режима Тренировка.

Две ключевые функции:
- generate_assignment_text() — персонализирует задание из ячейки
- evaluate_training_answer() — оценивает ответ по criteria + common_errors
"""

import json
import time
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
    TRAINING_THRESHOLDS_BY_DEPTH,
)

logger = get_logger(__name__)

BLOOM_LABELS = {
    'Remember': 'Запоминание',
    'Understand': 'Понимание',
    'Apply': 'Применение',
    'Analyze': 'Анализ',
    'Create': 'Создание',
}

# Language instructions for AI system prompts
_LANG_INSTRUCTIONS = {
    'ru': 'Формулируй на русском языке',
    'en': 'Write in English',
    'es': 'Escribe en español',
    'fr': 'Rédige en français',
    'zh': '用中文撰写',
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
    global _zp_cells_cache, _zp_cells_error_until
    if _zp_cells_cache is not None:
        return _zp_cells_cache

    # Suppress retries for 5 min after a load failure (prevents error log spam)
    if time.monotonic() < _zp_cells_error_until:
        return {}

    path = BASE_DIR / "data" / "curriculum" / "zp_cells.json"
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            logger.info(f"Loaded {len(data)} ZP cells from {path}")
            _zp_cells_cache = data
            return data
    except FileNotFoundError:
        logger.error(f"ZP cells file not found: {path} (BASE_DIR={BASE_DIR})")
        _zp_cells_error_until = time.monotonic() + 300
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"ZP cells JSON error: {e}")
        _zp_cells_error_until = time.monotonic() + 300
        return {}


_zp_cells_cache: dict = None
_zp_cells_error_until: float = 0  # monotonic timestamp: suppress retries for 5 min after failure


def load_kids_cells() -> dict:
    """Загрузить детские Z-ячейки из kids_cells.json.

    Источник: DS-principles-curriculum/data/curriculum/kids_cells.json
    Pack-источник: github.com/asf-denis-system/kids-learning-pack (Д. Асфандияров)
    Обновление: python scripts/extract_kids_cells.py в DS-principles-curriculum
    """
    global _kids_cells_cache, _kids_cells_error_until
    if _kids_cells_cache is not None:
        return _kids_cells_cache

    if time.monotonic() < _kids_cells_error_until:
        return {}

    path = BASE_DIR / "data" / "curriculum" / "kids_cells.json"
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            logger.info(f"Loaded {len(data)} kids cells from {path}")
            _kids_cells_cache = data
            return data
    except FileNotFoundError:
        logger.error(f"Kids cells file not found: {path}")
        _kids_cells_error_until = time.monotonic() + 300
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Kids cells JSON error: {e}")
        _kids_cells_error_until = time.monotonic() + 300
        return {}


_kids_cells_cache: dict = None
_kids_cells_error_until: float = 0


def get_principle_name(principle_id: str) -> str:
    """Получить имя принципа (из JSON или fallback)."""
    cells = load_zp_cells()
    name = cells.get(principle_id, {}).get('name')
    if name:
        return name
    return ZP_PRINCIPLE_NAMES.get(principle_id, principle_id)


def get_kid_principle_name(principle_id: str) -> str:
    """Получить имя детского принципа из kids_cells.json."""
    cells = load_kids_cells()
    return cells.get(principle_id, {}).get('name', principle_id)


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
    lang = 'ru'
    name = ''
    occupation = ''
    interests = ''
    account_id = intern.get('dt_user_id') if intern else None
    if intern:
        lang = intern.get('language', 'ru') or 'ru'
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
- {_LANG_INSTRUCTIONS.get(lang, _LANG_INSTRUCTIONS['ru'])}
- Длина: 3-7 предложений

ЦЕЛЕВЫЕ НАВЫКИ (can_do):
{chr(10).join(f'- {c}' for c in can_do)}

ШАБЛОН ЗАДАНИЯ (transfer_test):
{transfer_test}

ФОРМА ДЛЯ КОГНИТИВНОГО УРОВНЯ:
{form_text}"""

    _labels = {
        'ru': ("Создай задание для:", "пользователь", "занятие:", "интересы:"),
        'en': ("Create an assignment for:", "user", "occupation:", "interests:"),
        'es': ("Crea una tarea para:", "usuario", "ocupación:", "intereses:"),
        'fr': ("Crée un exercice pour :", "utilisateur", "occupation :", "intérêts :"),
        'zh': ("创建练习给：", "用户", "职业：", "兴趣："),
    }
    l = _labels.get(lang, _labels['ru'])
    user_prompt = f"{l[0]} {name or l[1]}"
    if occupation:
        user_prompt += f", {l[2]} {occupation}"
    if interests:
        user_prompt += f", {l[3]} {interests}"

    response = await claude.generate(
        system_prompt, user_prompt,
        max_tokens=500, model=CLAUDE_MODEL_HAIKU,
        account_id=account_id,
    )

    if not response:
        # Fallback: вернуть transfer_test напрямую
        return transfer_test or f"Выполните задание по принципу {principle_name} (глубина {depth})."

    return response


COGNITIVE_AGE_LABELS = {
    'preoperational': '3-6 лет',
    'concrete_operational': '7-11 лет',
    'formal_operational': '11-16 лет',
}


async def generate_child_assignment_text(
    cell_data: dict,
    cognitive_level: str,
    child_name: str,
    principle_name: str,
    depth: int,
    lang: str = 'ru',
    account_id: Optional[str] = None,
) -> str:
    """Сгенерировать карточку занятия для взрослого, который тренирует ребёнка.

    Args:
        cell_data: данные ячейки (одна глубина)
        cognitive_level: когнитивный уровень ребёнка
        child_name: имя ребёнка
        principle_name: название принципа
        depth: номер глубины (1-5)
        account_id: ory_id взрослого (не ребёнка — у детского профиля своего
            ory_id нет) для атрибуции стоимости в llm-proxy (AR.218)
    """
    forms = cell_data.get('forms', {})
    form_text = forms.get(cognitive_level, forms.get('concrete_operational', ''))
    transfer_test = cell_data.get('transfer_test', '')
    can_do = cell_data.get('can_do', [])
    domains = cell_data.get('domains', [])
    bloom_level = cell_data.get('bloom_level', '')
    bloom_label = BLOOM_LABELS.get(bloom_level, bloom_level)
    age_label = COGNITIVE_AGE_LABELS.get(cognitive_level, '7-11 лет')

    system_prompt = f"""Ты педагог-методист. Создай КАРТОЧКУ ЗАНЯТИЯ для взрослого, который будет тренировать принцип "{principle_name}" с ребёнком {child_name} ({age_label}).

Глубина {depth} ({bloom_label}).

ПРАВИЛА:
- Карточка — это ИНСТРУКЦИЯ ДЛЯ ВЗРОСЛОГО, не для ребёнка напрямую
- Занятие должно быть ОФЛАЙН-АКТИВНОСТЬЮ (игра, эксперимент, диалог, наблюдение)
- Адаптируй сложность под возраст: {age_label}
- Используй бытовые домены: {', '.join(domains)}
- Укажи: название занятия, длительность (5-20 мин), пошаговую инструкцию (3-5 шагов)
- В конце — критерий успеха (как понять, что ребёнок усвоил)
- НЕ используй Markdown с ** или __ (Telegram ломает)
- {_LANG_INSTRUCTIONS.get(lang, _LANG_INSTRUCTIONS['ru'])}
- Длина: 5-10 предложений

ФОРМАТ ЗАНЯТИЯ (из программы):
{form_text}

ШАБЛОН (transfer_test):
{transfer_test}

ЦЕЛЕВЫЕ НАВЫКИ (can_do):
{chr(10).join(f'- {c}' for c in can_do)}"""

    _child_labels = {
        'ru': f"Создай карточку занятия для ребёнка {child_name} ({age_label})",
        'en': f"Create an activity card for child {child_name} ({age_label})",
        'es': f"Crea una ficha de actividad para el niño {child_name} ({age_label})",
        'fr': f"Crée une fiche d'activité pour l'enfant {child_name} ({age_label})",
        'zh': f"为孩子{child_name}（{age_label}）创建活动卡片",
    }
    user_prompt = _child_labels.get(lang, _child_labels['ru'])

    response = await claude.generate(
        system_prompt, user_prompt,
        max_tokens=600, model=CLAUDE_MODEL_HAIKU,
        account_id=account_id,
    )

    if not response:
        return form_text or f"Проведите занятие по принципу {principle_name} (глубина {depth}) с ребёнком {child_name}."

    return response


async def evaluate_training_answer(
    answer_text: str,
    cell_data: dict,
    assignment_text: str,
    intern: Optional[dict],
    depth: int = 3,
) -> dict:
    """Оценить ответ пользователя через AI.

    Args:
        answer_text: текст ответа пользователя
        cell_data: данные ячейки
        assignment_text: текст задания
        intern: профиль пользователя
        depth: глубина (1-5), для первых уровней пороги ниже

    Returns:
        {passed: bool, partial: bool, feedback: str}
    """
    # Пороги зависят от глубины: на первых уровнях мягче
    depth_thresholds = TRAINING_THRESHOLDS_BY_DEPTH.get(depth)
    pass_threshold = depth_thresholds['pass'] if depth_thresholds else TRAINING_PASS_THRESHOLD
    partial_threshold = depth_thresholds['partial'] if depth_thresholds else TRAINING_PARTIAL_THRESHOLD

    criteria = cell_data.get('criteria', '')
    common_errors = cell_data.get('common_errors', [])
    can_do = cell_data.get('can_do', [])

    lang = 'ru'
    if intern:
        lang = intern.get('language', 'ru') or 'ru'

    errors_text = '\n'.join(
        f"- {e.get('error', '')}: {e.get('why', '')}" for e in common_errors
    )
    can_do_text = '\n'.join(f"- {c}" for c in can_do)

    _eval_labels = {
        'ru': {
            'role': 'Ты оцениваешь ответ на задание по тренировке мышления.',
            'assignment': 'ЗАДАНИЕ БЫЛО', 'criteria': 'КРИТЕРИИ ОЦЕНКИ',
            'skills': 'ЦЕЛЕВЫЕ НАВЫКИ', 'errors': 'ТИПИЧНЫЕ ОШИБКИ (проверь, не допустил ли ученик)',
            'evaluate': 'ОЦЕНИ ОТВЕТ и верни JSON',
            'feedback_hint': 'конструктивная обратная связь, 2-4 предложения',
            'passed_desc': 'ученик демонстрирует навыки из can_do',
            'partial_desc': 'частично верно, нужна доработка',
            'fail_desc': 'фундаментальное непонимание',
            'rules': 'ПРАВИЛА',
            'r_feedback': 'В feedback объясни ЧТО хорошо и ЧТО доработать',
            'r_error': 'Если обнаружил типичную ошибку — укажи её мягко',
            'r_json': 'Верни ТОЛЬКО JSON, без лишнего текста',
            'answer': 'ОТВЕТ УЧЕНИКА',
        },
        'en': {
            'role': 'You are evaluating a response to a thinking training assignment.',
            'assignment': 'THE ASSIGNMENT WAS', 'criteria': 'EVALUATION CRITERIA',
            'skills': 'TARGET SKILLS', 'errors': 'COMMON ERRORS (check if the student made them)',
            'evaluate': 'EVALUATE THE RESPONSE and return JSON',
            'feedback_hint': 'constructive feedback, 2-4 sentences',
            'passed_desc': 'student demonstrates skills from can_do',
            'partial_desc': 'partially correct, needs improvement',
            'fail_desc': 'fundamental misunderstanding',
            'rules': 'RULES',
            'r_feedback': 'In feedback explain WHAT is good and WHAT to improve',
            'r_error': 'If you found a common error — point it out gently',
            'r_json': 'Return ONLY JSON, no extra text',
            'answer': 'STUDENT RESPONSE',
        },
    }
    el = _eval_labels.get(lang, _eval_labels.get('en', _eval_labels['ru']))

    system_prompt = f"""{el['role']}

{el['assignment']}:
{assignment_text}

{el['criteria']}:
{criteria}

{el['skills']}:
{can_do_text}

{el['errors']}:
{errors_text}

{el['evaluate']}:
{{
  "score": <0.0-1.0>,
  "passed": <true if score >= {pass_threshold}>,
  "partial": <true if score >= {partial_threshold} and score < {pass_threshold}>,
  "feedback": "<{el['feedback_hint']}>"
}}

{_LANG_INSTRUCTIONS.get(lang, _LANG_INSTRUCTIONS['ru'])}

{el['rules']}:
- score >= {pass_threshold}: PASSED — {el['passed_desc']}
- score >= {partial_threshold}: PARTIAL — {el['partial_desc']}
- score < {partial_threshold}: FAIL — {el['fail_desc']}
- {el['r_feedback']}
- {el['r_error']}
- {el['r_json']}"""

    response = await claude.generate(
        system_prompt, f"{el['answer']}:\n{answer_text}",
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


async def generate_example_answer(
    cell_data: dict,
    assignment_text: str,
    principle_name: str,
    depth: int,
) -> str:
    """Сгенерировать пример правильного ответа (подсказка после 2 неудач).

    Args:
        cell_data: данные ячейки (одна глубина)
        assignment_text: текст задания
        principle_name: название принципа
        depth: номер глубины (1-5)

    Returns:
        Текст примера правильного ответа
    """
    criteria = cell_data.get('criteria', '')
    can_do = cell_data.get('can_do', [])
    can_do_text = '\n'.join(f'- {c}' for c in can_do)

    system_prompt = f"""Ты тренер мышления. Ученик дважды не справился с заданием по принципу "{principle_name}" (глубина {depth}).

ЗАДАНИЕ БЫЛО:
{assignment_text}

КРИТЕРИИ ОЦЕНКИ:
{criteria}

ЦЕЛЕВЫЕ НАВЫКИ:
{can_do_text}

Напиши КРАТКИЙ пример хорошего ответа на это задание.

ПРАВИЛА:
- Покажи ход рассуждения, а не просто ответ
- Длина: 3-5 предложений
- НЕ используй Markdown с ** или __ (Telegram ломает)
- Формулируй на русском языке
- Пример должен быть обучающим — чтобы ученик понял подход"""

    response = await claude.generate(
        system_prompt, "Напиши пример ответа.",
        max_tokens=400, model=CLAUDE_MODEL_HAIKU,
    )

    if not response:
        return "Попробуйте опереться на критерии оценки и целевые навыки задания."

    return response
