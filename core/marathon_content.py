"""
Loader контента марафона новичков (WP-330 Ф2.6 + Ф10.E v2 С9a).

Читает data/marathon-content.json при импорте и хранит в памяти.
Если файл отсутствует — использует fallback-шаблоны.

С9a (WP-330): добавлен routing по профилю (study_duration × complexity_level)
через resolve_variant. get_day_text принимает опциональный intern dict
и выбирает один из 4 вариантов: short_simple / short_complex / long_simple / long_complex.
"""

import json
import os
from typing import Optional

from config import get_logger

logger = get_logger(__name__)

_ROUTABLE_TYPES = {"lesson", "practice"}


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


_CONTENT: dict = _load_content()


def resolve_variant(study_duration, complexity_level) -> str:
    """Двухполевой ключ → суффикс варианта контента.

    Boundary: duration < 15 → short, duration >= 15 → long.
    None/'' для duration → дефолт 15 (long_simple).
    None/'' для complexity → дефолт 1 (simple).
    Range-строки ('5-10', '15-25') принимаются: берётся первое число.
    Любая ошибка cast → дефолт.

    Returns:
        'short_simple' | 'short_complex' | 'long_simple' | 'long_complex'
    """
    if study_duration is None or study_duration == "":
        duration_int = 15
    else:
        try:
            duration_str = str(study_duration).split("-")[0]
            duration_int = int(duration_str)
        except (ValueError, TypeError):
            duration_int = 15

    if complexity_level is None or complexity_level == "":
        complexity_int = 1
    else:
        try:
            complexity_int = int(complexity_level)
        except (ValueError, TypeError):
            complexity_int = 1

    bucket = "short" if duration_int < 15 else "long"
    style = "simple" if complexity_int <= 1 else "complex"
    return f"{bucket}_{style}"


def get_day_text(day: int, content_type: str, intern: Optional[dict] = None) -> Optional[str]:
    """Получить текст для дня и типа контента (с опциональным routing по профилю).

    Args:
        day: номер дня (1–14).
        content_type:
          - 'lesson' / 'practice': если intern передан с study_duration/complexity_level,
                                   делается routing на один из 4 вариантов
                                   (lesson_short_simple, lesson_long_complex и т.п.);
                                   без intern — fallback на legacy ключ 'lesson'/'practice'.
          - 'checkin' / 'reflection_question' / 'faq_hint': прямое чтение, без routing.
          - 'lesson_<bucket>_<style>' и зеркало для practice: прямое чтение варианта.
        intern: опциональный dict с полями 'study_duration', 'complexity_level'.

    Returns:
        Текст в Markdown или None, если ключ не найден.
    """
    day_key = str(day)
    if day_key not in _CONTENT:
        return None

    day_data = _CONTENT[day_key]
    resolved_type = content_type

    if resolved_type in _ROUTABLE_TYPES and intern is not None:
        variant = resolve_variant(
            intern.get("study_duration"),
            intern.get("complexity_level"),
        )
        variant_key = f"{resolved_type}_{variant}"
        text = day_data.get(variant_key)
        if text is not None:
            return text
        logger.warning(
            f"[MarathonContent] Variant key '{variant_key}' missing for day {day}, "
            f"falling back to legacy '{resolved_type}'"
        )
        return day_data.get(resolved_type)

    return day_data.get(resolved_type)


def get_all_days() -> dict:
    """Вернуть весь словарь дней (для тестов и дебага)."""
    return _CONTENT
