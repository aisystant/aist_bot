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


# Stored state when several states share the top score (pilot decision 15.11, WP-385 F18).
MIXED_STATE = 'mixed'

# Display labels of every value the /test can store (single source for profile, settings, progress).
STATE_LABELS = {
    'chaos': {'emoji': '😵', 'ru': 'Хаос', 'en': 'Chaos', 'es': 'Caos', 'fr': 'Chaos', 'zh': '混乱'},
    'deadlock': {'emoji': '🧱', 'ru': 'Тупик', 'en': 'Deadlock', 'es': 'Estancamiento', 'fr': 'Impasse', 'zh': '僵局'},
    'turning_point': {'emoji': '🔁', 'ru': 'Поворот', 'en': 'Turning Point', 'es': 'Punto de giro', 'fr': 'Tournant', 'zh': '转折点'},
    'development': {'emoji': '🌱', 'ru': 'Развитие', 'en': 'Development', 'es': 'Desarrollo', 'fr': 'Développement', 'zh': '发展'},
    MIXED_STATE: {'emoji': '🔀', 'ru': 'Смешанное', 'en': 'Mixed', 'es': 'Mixto', 'fr': 'Mixte', 'zh': '混合'},
    'unsure': {'emoji': '🤔', 'ru': 'Не определено', 'en': 'Unclear', 'es': 'No definido', 'fr': 'Indéterminé', 'zh': '不确定'},
}


def format_state_label(state_id: str, lang: str = 'ru') -> str:
    """'emoji Title' for a stored state; the raw id if the state is unknown."""
    info = STATE_LABELS.get(state_id)
    if not info:
        return state_id
    return f"{info['emoji']} {info.get(lang) or info['en']}"


def get_tied_groups(assessment: dict, scores: dict[str, int]) -> list[dict]:
    """Groups sharing the top score (> 0); one element means a single leader."""
    max_score = max(scores.values()) if scores else 0
    if max_score <= 0:
        return []
    return [g for g in assessment.get('groups', []) if scores.get(g['id'], 0) == max_score]


def resolve_state_id(assessment: dict, scores: dict[str, int]) -> str:
    """State id to store: the leader, 'mixed' on a tie, 'unsure' when all scores are zero."""
    if len(get_tied_groups(assessment, scores)) > 1:
        return MIXED_STATE
    return get_dominant_group(assessment, scores).get('id', '')


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
        'zh': '主导状态',
    }.get(lang, 'Dominant state')

    header = {
        'ru': 'Результат теста',
        'en': 'Test Result',
        'es': 'Resultado del test',
        'fr': 'Résultat du test',
        'zh': '测试结果',
    }.get(lang, 'Test Result')

    # Ничья: несколько состояний набрали максимум (>0) — показываем все, не одно «первое».
    tied = get_tied_groups(assessment, scores)
    if len(tied) > 1:
        tied_label = {
            'ru': 'Поровну', 'en': 'Tied', 'es': 'Empate', 'fr': 'Égalité', 'zh': '并列',
        }.get(lang, 'Tied')
        tied_text = " + ".join(
            f"{g.get('emoji', '')} *{_pick(g.get('title'), lang) or g['id']}*" for g in tied
        )
        return f"📊 *{header}*\n\n" + "\n".join(lines) + f"\n\n{tied_label}: {tied_text}"

    return (
        f"📊 *{header}*\n\n"
        + "\n".join(lines)
        + f"\n\n{result_label}: {dominant_emoji} *{dominant_title}*"
    )


def _pick(texts: dict, lang: str) -> str:
    """Текст на языке пользователя; запасной — русский, затем английский."""
    if not isinstance(texts, dict):
        return ''
    return texts.get(lang) or texts.get('ru') or texts.get('en') or ''


def format_states_legend(assessment: dict, lang: str = 'ru') -> str:
    """Краткое описание всех состояний теста + подсказка «смотрите, чего больше».

    Данные берутся из YAML: у каждой группы поле summary (одна строка),
    у теста — result_legend_title и result_hint. Если summary нет ни у одной
    группы, возвращается пустая строка (старые тесты без легенды не ломаются).
    """
    lines = []
    for group in assessment.get('groups', []):
        summary = _pick(group.get('summary'), lang)
        if not summary:
            continue
        title = _pick(group.get('title'), lang) or group.get('id', '')
        lines.append(f"{group.get('emoji', '')} *{title}* — {summary}")

    if not lines:
        return ''

    legend_title = _pick(assessment.get('result_legend_title'), lang)
    hint = _pick(assessment.get('result_hint'), lang)

    parts = []
    if legend_title:
        parts.append(f"ℹ️ *{legend_title}*")
    parts.append("\n".join(lines))
    if hint:
        parts.append(f"👀 {hint}")
    return "\n\n".join(parts)
