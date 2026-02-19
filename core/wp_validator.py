"""
Валидатор формулировки рабочего продукта (DS-evaluator-agent runtime).

Двухуровневая проверка:
- Level 1: regex + стоп-слова (0 мс, без LLM)
- Level 2: Claude Haiku (fallback для неоднозначных случаев, <2 сек)

Правило: РП начинается с существительного (артефакт или отглагольное),
НЕ с глагола, процесса, метода или желания.

Промпт: DS-evaluator-agent/prompts/wp-validate.md
"""

import re
from typing import Optional

from config import get_logger, CLAUDE_MODEL_HAIKU

logger = get_logger(__name__)


# ─── Level 1: Regex-проверка ───

# Стоп-слова в начале формулировки (процесс, желание, действие)
BAD_STARTS_RE = re.compile(
    r'^(хочу|надо|нужно|сделать|делать|работа\b|процесс\b|метод\b|способ\b|задача\b'
    r'|попробовать|попытаться|начать|продолжить'
    r'|я\s|мне\s|мой\s|моя\s|моё\s)',
    re.IGNORECASE,
)

# Инфинитив: первое слово оканчивается на глагольные суффиксы
VERB_INFINITIVE_RE = re.compile(
    r'^[а-яёА-ЯЁ]+?(ать|ять|ить|еть|уть|ыть|чь|ти|сти|зти|ться|тись)\s',
    re.IGNORECASE,
)

# Одиночный инфинитив (без пробела после — вся формулировка одно слово-глагол)
VERB_SINGLE_RE = re.compile(
    r'^[а-яёА-ЯЁ]+?(ать|ять|ить|еть|уть|ыть|чь|ти|сти|зти|ться|тись)$',
    re.IGNORECASE,
)

# Белый список: артефактные существительные в начале
GOOD_STARTS = {
    'чек-лист', 'список', 'таблица', 'схема', 'план', 'текст', 'пост',
    'описание', 'определение', 'формулировка', 'карта', 'матрица',
    'диаграмма', 'набор', 'реестр', 'анализ', 'обзор', 'отчёт',
    'протокол', 'шаблон', 'черновик', 'заметка', 'конспект', 'выписка',
    'перечень', 'классификация', 'сравнение', 'оценка', 'прогноз',
    # Отглагольные существительные (частые)
    'обогащение', 'масштабирование', 'интеграция', 'публикация',
    'развёртывание', 'описание', 'создание', 'разработка', 'проектирование',
    'тестирование', 'исследование', 'моделирование', 'диагностика',
}


def _get_first_word(text: str) -> str:
    """Извлекает первое слово из текста (lowercase)."""
    return text.strip().split()[0].lower() if text.strip() else ""


def validate_formulation_regex(text: str) -> dict:
    """Level 1: быстрая regex-проверка формулировки РП.

    Returns:
        {"valid": bool, "reason": str, "confident": bool}
        confident=True означает, что regex уверен в результате.
        confident=False означает, что нужен Level 2 (Claude).
    """
    text = text.strip()
    if not text:
        return {"valid": False, "reason": "empty", "confident": True}

    first_word = _get_first_word(text)

    # Белый список — точно хорошо
    if first_word in GOOD_STARTS:
        return {"valid": True, "reason": "good_start", "confident": True}

    # Стоп-слова — точно плохо
    if BAD_STARTS_RE.match(text):
        return {"valid": False, "reason": "bad_start", "confident": True}

    # Инфинитив — точно плохо
    if VERB_INFINITIVE_RE.match(text) or VERB_SINGLE_RE.match(text):
        return {"valid": False, "reason": "verb_infinitive", "confident": True}

    # Отглагольные существительные с типичными суффиксами — скорее хорошо
    if re.match(r'^[а-яёА-ЯЁ]+?(ание|ение|ция|ка|ость|ство)\b', text, re.IGNORECASE):
        return {"valid": True, "reason": "deverbal_noun", "confident": True}

    # Неоднозначно — передаём на Level 2
    return {"valid": True, "reason": "unknown", "confident": False}


async def validate_formulation_llm(text: str) -> dict:
    """Level 2: Claude проверка для неоднозначных случаев.

    Returns:
        {"valid": bool, "suggestion": str}
    """
    from clients.claude import claude

    system_prompt = """Определи, является ли формулировка рабочего продукта корректной.

ПРАВИЛО: Рабочий продукт — материально зафиксированный артефакт.
Формулировка должна начинаться с существительного (артефакт или отглагольное),
а НЕ с глагола, процесса, метода или желания.

ХОРОШО: «Чек-лист привычек», «Описание текущего состояния», «Таблица целей», «Обогащение базы знаний»
ПЛОХО: «Проанализировать привычки», «Работа над планом», «Хочу внедрить», «Метод анализа»

Ответь СТРОГО в формате:
VALID: true или false
SUGGESTION: <если false — предложи исправленную формулировку>"""

    user_prompt = f'Формулировка: «{text}»'

    try:
        raw = await claude.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=150,
            model=CLAUDE_MODEL_HAIKU,
        )
        if not raw:
            return {"valid": True, "suggestion": ""}  # при ошибке — пропускаем

        valid = True
        suggestion = ""
        for line in raw.split('\n'):
            line = line.strip()
            if line.startswith('VALID:'):
                valid = 'true' in line.lower()
            elif line.startswith('SUGGESTION:'):
                suggestion = line[11:].strip()

        return {"valid": valid, "suggestion": suggestion}

    except Exception as e:
        logger.error(f"WP validator LLM error: {e}")
        return {"valid": True, "suggestion": ""}  # при ошибке — пропускаем


async def validate_formulation(text: str, use_llm_fallback: bool = True) -> dict:
    """Полная валидация формулировки РП (Level 1 + опциональный Level 2).

    Args:
        text: формулировка рабочего продукта
        use_llm_fallback: использовать Claude для неоднозначных случаев

    Returns:
        {"valid": bool, "reason": str, "suggestion": str}
    """
    # Level 1: regex
    result = validate_formulation_regex(text)

    if result["confident"]:
        return {
            "valid": result["valid"],
            "reason": result["reason"],
            "suggestion": "",
        }

    # Level 2: Claude (если regex не уверен)
    if use_llm_fallback:
        llm_result = await validate_formulation_llm(text)
        return {
            "valid": llm_result["valid"],
            "reason": "llm_check",
            "suggestion": llm_result.get("suggestion", ""),
        }

    # Без LLM — пропускаем неоднозначные
    return {"valid": True, "reason": "skipped_llm", "suggestion": ""}


def get_wp_hint(bloom_level: int, text: str, suggestion: str = "", lang: str = "ru") -> str:
    """Генерирует подсказку по формулировке РП в зависимости от Bloom-уровня.

    Args:
        bloom_level: 1-3
        text: оригинальная формулировка
        suggestion: предложенное исправление (из LLM)
        lang: язык

    Returns:
        Текст подсказки для пользователя
    """
    bl = max(1, min(bloom_level, 3))
    example = suggestion or "Чек-лист привычек"

    if lang != "ru":
        # Для не-русских языков — упрощённая подсказка
        return f"Next time, try naming the result (artifact). For example: *{example}*"

    if bl == 1:
        return (
            f"Попробуй в будущем назвать *результат*. "
            f"Например: *{example}* или *Таблица целей*. "
            f"Что именно получится в итоге?"
        )
    elif bl == 2:
        return (
            f"Рабочий продукт — это *артефакт*, который можно показать. "
            f"«{text}» звучит как действие. "
            f"Попробуй в будущем написать: *{example}*"
        )
    else:
        return (
            f"По правилу РАБОЧИЙ ПРОДУКТ ≠ ДЕЙСТВИЕ: "
            f"РП формулируется существительным. "
            f"«{text}» — это процесс. Попробуй в будущем: *{example}*"
        )
