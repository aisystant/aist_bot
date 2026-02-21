"""
Детектор personal query для проактивной инъекции данных Цифрового Двойника.

Определяет, является ли вопрос пользователя персональным (о его целях,
проблемах, интересах и т.д.) и возвращает пути ЦД для предзагрузки.

Данные загружаются ДО вызова Claude и инжектятся в system prompt,
что исключает лишний tool round (~2-4 сек экономии).
"""

import asyncio
import json
from typing import Any, Dict, List, Optional

from config import get_logger

logger = get_logger(__name__)


# =========================================================================
# PERSONAL QUERY PATTERNS → DT PATHS
# =========================================================================

# Каждая категория: patterns (подстроки для lowercase match) → paths (DT metamodel)
PERSONAL_PATTERNS: Dict[str, Dict[str, Any]] = {
    'goals': {
        'patterns': [
            "мои цели", "моя цель", "цели обучения", "зачем я учусь",
            "чего хочу достичь", "мой план обучения",
            "my goals", "my goal", "learning goals", "what i want to achieve",
        ],
        'paths': ["1_declarative/1_2_goals/09_Цели обучения"],
        'label': "Цели обучения",
    },
    'problems': {
        'patterns': [
            "мои проблемы", "что мне мешает", "мои трудности",
            "мои сложности", "что тормозит", "мои препятствия",
            "my problems", "what blocks me", "my difficulties",
        ],
        'paths': ["1_declarative/1_4_context/01_Текущие проблемы"],
        'label': "Текущие проблемы",
    },
    'dissatisfactions': {
        'patterns': [
            "неудовлетвор", "чем недоволен", "что не устраивает",
            "чем я не доволен", "мои боли",
            "dissatisf", "what bothers me",
        ],
        'paths': ["1_declarative/1_4_context/04_Мои неудовлетворенности"],
        'label': "Неудовлетворённости",
    },
    'interests': {
        'patterns': [
            "мои интересы", "что мне интересно", "интересующие области",
            "my interests", "what interests me",
        ],
        'paths': ["1_declarative/1_2_goals/05_Интересующие области"],
        'label': "Интересы",
    },
    'roles': {
        'patterns': [
            "моя роль", "мои роли", "кем я работаю", "кем работаю",
            "my role", "my roles",
        ],
        'paths': ["1_declarative/1_3_selfeval/12_Текущие роли"],
        'label': "Роли",
    },
    'projects': {
        'patterns': [
            "мои проект", "над чем работаю", "мои задачи",
            "my project", "what i work on",
        ],
        'paths': ["1_declarative/1_2_goals/11_Мои приоритетные проекты"],
        'label': "Проекты",
    },
    'emotions': {
        'patterns': [
            "мои эмоции", "что чувствую", "моё состояние",
            "как я себя чувствую", "мои чувства",
            "my emotions", "how i feel", "my feelings",
        ],
        'paths': ["1_declarative/1_4_context/06_Мои эмоции и чувства"],
        'label': "Эмоции и состояние",
    },
    'methods': {
        'patterns': [
            "мои методы", "мои фокусные методы", "какие методы я изучаю",
            "my methods", "my focus methods",
        ],
        'paths': ["1_declarative/1_3_selfeval/08_Фокусные методы"],
        'label': "Фокусные методы",
    },
    'full_profile': {
        'patterns': [
            "мой профиль", "обо мне", "расскажи обо мне",
            "всё о мне", "мои данные в двойнике",
            "my profile", "about me", "tell me about myself",
        ],
        'paths': ["1_declarative"],
        'label': "Полный профиль",
    },
}


def detect_personal_query(question: str) -> List[str]:
    """Определяет, является ли вопрос персональным, и возвращает DT paths.

    Args:
        question: вопрос пользователя

    Returns:
        Список уникальных DT paths для предзагрузки. Пустой = не personal query.
    """
    q = question.lower()
    paths: List[str] = []
    matched_categories: List[str] = []

    for category, config in PERSONAL_PATTERNS.items():
        for pattern in config['patterns']:
            if pattern in q:
                for path in config['paths']:
                    if path not in paths:
                        paths.append(path)
                matched_categories.append(category)
                break  # Одного match в категории достаточно

    if paths:
        logger.info(
            f"Personal query detected: categories={matched_categories}, "
            f"paths={len(paths)}"
        )

    return paths


async def fetch_dt_context(
    telegram_user_id: int,
    paths: List[str],
) -> str:
    """Загружает данные ЦД по путям и форматирует как контекст.

    Args:
        telegram_user_id: ID пользователя Telegram
        paths: DT paths для загрузки

    Returns:
        Отформатированная строка для system prompt. Пустая если данных нет.
    """
    from clients.digital_twin import digital_twin

    if not digital_twin.is_connected(telegram_user_id):
        return ""

    # Параллельный fetch всех путей
    async def _fetch_one(path: str) -> Optional[tuple]:
        try:
            data = await digital_twin.read(path, telegram_user_id)
            if data is not None:
                return (path, data)
        except Exception as e:
            logger.warning(f"DT fetch error for path '{path}': {e}")
        return None

    results = await asyncio.gather(
        *[_fetch_one(p) for p in paths],
        return_exceptions=True,
    )

    # Форматируем результаты
    sections: List[str] = []
    for result in results:
        if isinstance(result, Exception):
            logger.warning(f"DT fetch exception: {result}")
            continue
        if result is None:
            continue

        path, data = result
        formatted = _format_dt_data(path, data)
        if formatted:
            sections.append(formatted)

    if not sections:
        return ""

    header = "ДАННЫЕ ЦИФРОВОГО ДВОЙНИКА ПОЛЬЗОВАТЕЛЯ"
    body = "\n".join(sections)

    logger.info(
        f"DT context injected: {len(sections)} sections, "
        f"{len(body)} chars for user {telegram_user_id}"
    )

    return f"{header}:\n{body}"


def _format_dt_data(path: str, data: Any) -> str:
    """Форматирует данные одного DT path для включения в prompt.

    Args:
        path: DT path
        data: данные (dict, list, str, etc.)

    Returns:
        Отформатированная строка или пустая строка
    """
    # Определяем label из PERSONAL_PATTERNS
    label = path
    for config in PERSONAL_PATTERNS.values():
        if path in config['paths']:
            label = config['label']
            break

    if data is None:
        return ""

    if isinstance(data, dict):
        # Для dict — рекурсивно собираем ключ: значение
        parts = []
        for key, value in data.items():
            if value is None or value == "" or value == []:
                continue
            if isinstance(value, list):
                value_str = ", ".join(str(v) for v in value)
            elif isinstance(value, dict):
                value_str = json.dumps(value, ensure_ascii=False, default=str)
            else:
                value_str = str(value)
            # Убираем числовые префиксы из ключей (01_Текущие проблемы → Текущие проблемы)
            clean_key = key.lstrip("0123456789_") if key[:1].isdigit() else key
            parts.append(f"  - {clean_key}: {value_str}")
        if not parts:
            return ""
        return f"• {label}:\n" + "\n".join(parts)

    if isinstance(data, list):
        if not data:
            return ""
        items = ", ".join(str(v) for v in data)
        return f"• {label}: {items}"

    text = str(data).strip()
    if not text:
        return ""
    return f"• {label}: {text}"
