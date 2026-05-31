"""
Loader контента марафона новичков (WP-330 Ф2.6).

Читает data/marathon-content.json при импорте и хранит в памяти.
Если файл отсутствует — использует fallback-шаблоны.
"""

import json
import os
from typing import Optional

from config import get_logger

logger = get_logger(__name__)

# Module-level cache
_CONTENT: dict = {}


def _load_content() -> dict:
    """Прочитать marathon-content.json из data/ или вернуть пустой dict."""
    paths = [
        os.path.join(os.path.dirname(__file__), "..", "data", "marathon-content.json"),
        os.path.join(os.path.dirname(__file__), "..", "..", "DS-marathon-v2-tseren", "materials", "participants", "marathon-content.json"),
    ]
    for path in paths:
        path = os.path.abspath(path)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"[MarathonContent] Loaded from {path}")
                return data.get("days", {})
            except Exception as e:
                logger.warning(f"[MarathonContent] Failed to load {path}: {e}")
    logger.warning("[MarathonContent] No content file found, using fallbacks")
    return {}


def get_day_text(day: int, content_type: str) -> Optional[str]:
    """Получить текст для дня и типа контента.

    Args:
        day: номер дня (1–14)
        content_type: legacy: 'lesson', 'practice', 'checkin'.
                       WP-330 Ф10.B: 'lesson_full', 'practice_full',
                       'reflection_question', 'faq_hint'.

    Returns:
        Текст в Markdown или None, если не найден.
    """
    global _CONTENT
    if not _CONTENT:
        _CONTENT = _load_content()

    day_key = str(day)
    if day_key not in _CONTENT:
        return None

    return _CONTENT[day_key].get(content_type)


def get_all_days() -> dict:
    """Вернуть весь словарь дней (для тестов и дебага)."""
    global _CONTENT
    if not _CONTENT:
        _CONTENT = _load_content()
    return _CONTENT
