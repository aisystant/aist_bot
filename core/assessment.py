"""
Движок опросников.

Загружает тесты из YAML, управляет прохождением,
подсчитывает баллы, форматирует результат.

Добавление нового теста = добавление YAML в config/assessments/.
"""

from pathlib import Path
from typing import Optional

import yaml

from config import get_logger

logger = get_logger(__name__)

ASSESSMENTS_DIR = Path(__file__).parent.parent / "config" / "assessments"


def load_assessment(assessment_id: str) -> Optional[dict]:
    """Загрузить тест из YAML по ID.

    Args:
        assessment_id: ID теста (например, 'systematicity')

    Returns:
        Словарь с данными теста или None
    """
    path = ASSESSMENTS_DIR / f"{assessment_id}.yaml"
    if not path.exists():
        logger.error(f"Assessment file not found: {path}")
        return None

    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def get_question(assessment: dict, index: int) -> Optional[dict]:
    """Получить вопрос по индексу.

    Returns:
        Словарь вопроса или None если индекс за пределами
    """
    questions = assessment.get('questions', [])
    if 0 <= index < len(questions):
        return questions[index]
    return None


def get_total_questions(assessment: dict) -> int:
    """Общее количество вопросов."""
    return len(assessment.get('questions', []))


def calculate_scores(assessment: dict, answers: dict[str, bool]) -> dict[str, int]:
    """Подсчитать баллы по группам.

    Args:
        assessment: данные теста
        answers: {question_id: True/False}

    Returns:
        {group_id: score}
    """
    scores = {}
    for group in assessment.get('groups', []):
        scores[group['id']] = 0

    for question in assessment.get('questions', []):
        qid = question['id']
        group = question['group']
        if answers.get(qid):
            scores[group] = scores.get(group, 0) + 1

    return scores


def get_dominant_group(assessment: dict, scores: dict[str, int]) -> dict:
    """Определить преобладающую группу.

    Returns:
        Словарь группы с максимальным баллом.
        Если все баллы равны 0 (ничья / все ответы «Нет»),
        возвращает группу-заглушку с id='unsure'.
    """
    groups = assessment.get('groups', [])
    if not groups:
        return {}

    # UX-audit Day 1 №1: явно разрешаем ничью «все нули».
    if scores and max(scores.values()) == 0:
        return {'id': 'unsure', 'emoji': '🤔', 'title': {'ru': 'Не определено', 'en': 'Unclear'}}

    max_score = -1
    dominant = groups[0]

    for group in groups:
        score = scores.get(group['id'], 0)
        if score > max_score:
            max_score = score
            dominant = group

    return dominant


def get_max_per_group(assessment: dict) -> int:
    """Максимальный балл на группу (количество вопросов в группе)."""
    groups = assessment.get('groups', [])
    if not groups:
        return 0

    counts = {}
    for q in assessment.get('questions', []):
        g = q['group']
        counts[g] = counts.get(g, 0) + 1

    return max(counts.values()) if counts else 0


def format_progress_bar(current: int, total: int, length: int = 12) -> str:
    """Прогресс-бар для текущего вопроса.

    Args:
        current: текущий вопрос (1-based)
        total: всего вопросов
        length: длина полоски в символах
    """
    filled = round(current / total * length)
    return "▓" * filled + "░" * (length - filled)


def format_score_bar(score: int, max_score: int) -> str:
    """Визуальная полоска баллов.

    Args:
        score: набранные баллы
        max_score: максимум
    """
    filled = "█" * score
    empty = "░" * (max_score - score)
    return filled + empty


def format_result(assessment: dict, scores: dict[str, int], lang: str = 'ru') -> str:
    """Форматировать результат теста для отображения.

    Args:
        assessment: данные теста
        scores: баллы по группам
        lang: язык

    Returns:
        Отформатированная строка результата
    """
    max_per_group = get_max_per_group(assessment)
    dominant = get_dominant_group(assessment, scores)

    lines = []
    for group in assessment.get('groups', []):
        gid = group['id']
        score = scores.get(gid, 0)
        emoji = group.get('emoji', '')
        title = group.get('title', {}).get(lang, group.get('title', {}).get('ru', gid))
        bar = format_score_bar(score, max_per_group)
        lines.append(f"{emoji} {title}:  {bar}  {score}/{max_per_group}")

    dominant_emoji = dominant.get('emoji', '')
    dominant_title = dominant.get('title', {}).get(lang, dominant.get('title', {}).get('ru', ''))

    result_label = {
        'ru': 'Преобладающее состояние',
        'en': 'Dominant state',
        'es': 'Estado predominante',
        'fr': 'État prédominant',
    }.get(lang, 'Dominant state')

    header = {
        'ru': 'Результат теста',
        'en': 'Test Result',
        'es': 'Resultado del test',
        'fr': 'Résultat du test',
    }.get(lang, 'Test Result')

    return (
        f"📊 *{header}*\n\n"
        + "\n".join(lines)
        + f"\n\n{result_label}: {dominant_emoji} *{dominant_title}*"
    )
