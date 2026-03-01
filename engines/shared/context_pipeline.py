"""
Tier Context Pipeline — сборка контекста для system prompt по тиру.

Каждый collector — async функция, возвращающая (placeholder_key, value).
TIER_PIPELINE определяет, какие collectors запускаются для каждого тира.
Collectors запускаются параллельно через asyncio.gather.

Архитектурное решение (DP.ARCH.002):
- T1 Expert: user_profile + bot_context
- T2 Mentor: + standard_claude
- T3 Co-thinker: + personal_claude
- T4 Architect: = T3 (future: + progress, plans)
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from config import get_logger

logger = get_logger(__name__)

# Type: collector returns (template_key, value_string)
CollectorResult = Tuple[str, str]


# =============================================================================
# COLLECTORS
# =============================================================================

async def collect_user_profile(
    intern: dict, lang: str, **kwargs
) -> CollectorResult:
    """Профиль пользователя из bot DB → {user_profile}."""
    from .question_handler import _build_user_profile
    return ("user_profile", _build_user_profile(intern, lang))


async def collect_bot_context(
    intern: dict, lang: str, bot_context: str = "", **kwargs
) -> CollectorResult:
    """Self-knowledge бота → {bot_section}."""
    if not bot_context:
        return ("bot_section", "")
    section = (
        f"\n\nЗНАНИЯ О БОТЕ:\n{bot_context}\n"
        "Если вопрос касается бота — отвечай ТОЛЬКО на основе информации выше."
    )
    return ("bot_section", section)


async def collect_standard_claude(
    intern: dict, lang: str, **kwargs
) -> CollectorResult:
    """Standard CLAUDE.md (методология) → {standard_section}."""
    from .consultation_tools import get_standard_claude_md
    standard_claude = get_standard_claude_md()
    if not standard_claude:
        return ("standard_section", "")
    return ("standard_section", f"\n\nМЕТОДОЛОГИЯ:\n{standard_claude}")


async def collect_personal_claude(
    intern: dict, lang: str, personal_claude_md: str = "", **kwargs
) -> CollectorResult:
    """Personal CLAUDE.md из GitHub → {personal_section}."""
    if not personal_claude_md:
        return ("personal_section", "")
    return ("personal_section", f"\n\nПЕРСОНАЛЬНЫЙ КОНТЕКСТ ПОЛЬЗОВАТЕЛЯ:\n{personal_claude_md}")


async def collect_user_progress(
    intern: dict, lang: str, **kwargs
) -> CollectorResult:
    """Прогресс пользователя в обучении из bot DB → {progress_section}.

    Данные берутся из intern dict (уже загружен из БД, 0 доп. запросов).
    Включает: статус марафона, текущий день, пройденные темы, серию дней.
    """
    marathon_status = intern.get('marathon_status', 'not_started')
    if marathon_status == 'not_started':
        return ("progress_section", "")

    parts = []

    # Статус марафона
    status_labels = {
        'active': 'Активен',
        'paused': 'На паузе',
        'completed': 'Завершён',
    }
    parts.append(f"Статус марафона: {status_labels.get(marathon_status, marathon_status)}")

    # Текущий день / индекс темы
    current_topic_index = intern.get('current_topic_index', 0)
    if current_topic_index is not None:
        parts.append(f"Текущая тема: #{current_topic_index + 1}")

    # Пройденные темы
    completed = intern.get('completed_topics', [])
    if isinstance(completed, str):
        import json as _json
        try:
            completed = _json.loads(completed)
        except (ValueError, TypeError):
            completed = []
    if completed:
        parts.append(f"Пройдено тем: {len(completed)}")

    # Серия активных дней
    streak = intern.get('active_days_streak', 0)
    if streak and streak > 0:
        parts.append(f"Серия активных дней: {streak}")

    # Дата старта
    start_date = intern.get('marathon_start_date')
    if start_date:
        parts.append(f"Дата начала: {start_date}")

    if not parts:
        return ("progress_section", "")

    header = "ПРОГРЕСС ПОЛЬЗОВАТЕЛЯ" if lang != 'en' else "USER PROGRESS"
    body = "\n".join(f"- {p}" for p in parts)
    return ("progress_section", f"\n{header}:\n{body}")


# =============================================================================
# TIER PIPELINE CONFIG
# =============================================================================

TIER_PIPELINE: Dict[int, List] = {
    1: [collect_user_profile, collect_bot_context, collect_user_progress],
    2: [collect_user_profile, collect_bot_context, collect_standard_claude, collect_user_progress],
    3: [collect_user_profile, collect_bot_context, collect_standard_claude, collect_personal_claude, collect_user_progress],
    4: [collect_user_profile, collect_bot_context, collect_standard_claude, collect_personal_claude, collect_user_progress],
}


# =============================================================================
# ASSEMBLER
# =============================================================================

async def assemble_context(
    tier: int,
    intern: dict,
    lang: str,
    bot_context: str = "",
    personal_claude_md: str = "",
) -> Dict[str, str]:
    """Запускает collectors для тира параллельно, возвращает dict placeholder → value.

    Args:
        tier: тир обслуживания (1-4)
        intern: профиль пользователя из bot DB
        lang: язык пользователя
        bot_context: self-knowledge бота
        personal_claude_md: personal CLAUDE.md из GitHub

    Returns:
        Dict с ключами для fill_tier_prompt:
        {user_profile, bot_section, standard_section, personal_section, dynamic_sections}
    """
    collectors = TIER_PIPELINE.get(tier, TIER_PIPELINE[1])

    # Общие kwargs для всех collectors
    ctx = dict(
        intern=intern,
        lang=lang,
        bot_context=bot_context,
        personal_claude_md=personal_claude_md,
    )

    # Запускаем параллельно
    results: List[CollectorResult] = await asyncio.gather(
        *[c(**ctx) for c in collectors],
        return_exceptions=True,
    )

    # Собираем результаты
    sections: Dict[str, str] = {
        "user_profile": "",
        "bot_section": "",
        "standard_section": "",
        "personal_section": "",
        "dynamic_sections": "",
        "progress_section": "",
    }

    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Context collector error: {result}")
            continue
        key, value = result
        sections[key] = value

    logger.info(
        f"Context pipeline T{tier}: {len(collectors)} collectors, "
        f"{sum(1 for v in sections.values() if v)} non-empty sections"
    )
    return sections
