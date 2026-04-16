"""
Tier Context Pipeline — сборка контекста для system prompt по тиру.

Каждый collector — async функция, возвращающая (placeholder_key, value).
TIER_PIPELINE определяет, какие collectors запускаются для каждого тира.
Collectors запускаются параллельно через asyncio.gather.

Pre-search (search-first pattern):
- collect_pre_search вызывает knowledge-mcp ДО Claude, результаты в {knowledge_section}.
- Гарантирует, что Claude видит релевантные документы даже без вызова tool.
- Решает проблему: Claude мог не вызвать search_knowledge и отвечать "не знаю".

Архитектурное решение (DP.ARCH.002):
- T1 Expert: user_profile + bot_context + pre_search
- T2 Mentor: + standard_claude + iwe_instructions
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


async def collect_iwe_instructions(
    intern: dict, lang: str, **kwargs
) -> CollectorResult:
    """IWE platform instructions через Gateway get_instructions → {iwe_section}.

    Даёт Claude знания о платформе IWE: что такое IWE, как подключить клиентов
    (Cursor, Claude.ai, ChatGPT, Claude Code), что такое Pack, Gateway и т.д.
    Без этого бот не может ответить на вопросы про IWE-инфраструктуру.

    WP-209 Ф2b: бот как консультант по IWE.
    """
    try:
        from clients.gateway_mcp import gateway_mcp

        # get_instructions — публичный tool, не требует auth.
        # Вызываем без telegram_user_id, чтобы не слать протухший Bearer token.
        instructions = await gateway_mcp.get_instructions()

        if not instructions:
            return ("iwe_section", "")

        # IWE_SYSTEM_PROMPT ~11.5KB. Берём до 6000 символов —
        # достаточно для core rules, tools, roles, onboarding.
        truncated = instructions[:6000]
        section = (
            f"\n\nИНСТРУКЦИИ ПЛАТФОРМЫ IWE:\n{truncated}\n"
            "Используй эту информацию для ответов о платформе IWE, "
            "подключении клиентов, архитектуре и возможностях."
        )
        logger.info(f"IWE instructions loaded: {len(truncated)} chars")
        return ("iwe_section", section)
    except Exception as e:
        logger.warning(f"IWE instructions error: {e}")
        return ("iwe_section", "")


async def collect_pre_search(
    intern: dict, lang: str, question: str = "", **kwargs
) -> CollectorResult:
    """Pre-search: вызывает knowledge-mcp ДО Claude → {knowledge_section}.

    Гарантирует, что Claude видит релевантные документы из базы знаний
    без необходимости вызывать tool. Решает проблему routing reliability:
    Claude мог не вызвать search_knowledge и ответить "такой возможности нет".
    """
    if not question:
        return ("knowledge_section", "")

    try:
        from clients.gateway_mcp import gateway_mcp

        telegram_user_id = intern.get('chat_id')
        results = await gateway_mcp.knowledge_search(
            query=question, limit=5, telegram_user_id=telegram_user_id
        )

        # B4.20: knowledge_search уже делает fallback внутри при token_expired,
        # но если results пустые по другой причине — не пытаемся снова.
        if not results:
            return ("knowledge_section", "")

        # Форматируем компактно
        parts = []
        for item in results[:5]:
            if isinstance(item, dict):
                content = item.get('content', item.get('text', ''))
                source = item.get('source', '')
                filename = item.get('filename', '')
                if content:
                    header = f"[{filename}]" if filename else f"[{source}]"
                    parts.append(f"{header}\n{content[:1500]}")

        if not parts:
            return ("knowledge_section", "")

        context = "\n\n---\n\n".join(parts)
        section = (
            f"\n\nИНФОРМАЦИЯ ИЗ БАЗЫ ЗНАНИЙ (pre-search):\n{context}\n\n"
            "Используй эту информацию для ответа. Если нужно больше деталей — "
            "вызови search_knowledge или search_guides для уточняющего поиска."
        )
        auth_note = "" if telegram_user_id else " [anon fallback]"
        logger.info(f"Pre-search{auth_note}: {len(parts)} results, {len(section)} chars for question '{question[:50]}...'")
        return ("knowledge_section", section)
    except Exception as e:
        logger.warning(f"Pre-search error: {e}")
        return ("knowledge_section", "")


async def collect_user_progress(
    intern: dict, lang: str, ui_tier: int = -1, **kwargs
) -> CollectorResult:
    """Прогресс пользователя в обучении из bot DB → {progress_section}.

    Данные берутся из intern dict (уже загружен из БД, 0 доп. запросов).
    Показывает прогресс ВСЕГДА, если есть хоть какие-то данные:
    - UITier (текущий уровень платформы)
    - Активность (дни, серия, рекорд)
    - Марафон (статус, день, темы)
    - Лента (статус)
    - Дата регистрации
    """
    parts = []

    # --- UITier (anti-hallucination: Claude должен знать точный тир) ---
    if ui_tier >= 0:
        from core.tier_config import TIER_DISPLAY
        tier_label = TIER_DISPLAY.get(ui_tier, f"T{ui_tier}")
        parts.append(f"Текущий уровень платформы: {tier_label}")

    # --- Активность (показываем всегда, если есть) ---
    total_days = intern.get('active_days_total', 0)
    streak = intern.get('active_days_streak', 0)
    longest = intern.get('longest_streak', 0)
    last_active = intern.get('last_active_date')

    if total_days and total_days > 0:
        parts.append(f"Всего активных дней: {total_days}")
    if streak and streak > 0:
        parts.append(f"Текущая серия: {streak} дн.")
    if longest and longest > 0 and longest != streak:
        parts.append(f"Рекорд серии: {longest} дн.")
    if last_active:
        parts.append(f"Последняя активность: {last_active}")

    # --- Марафон ---
    marathon_status = intern.get('marathon_status', 'not_started')
    if marathon_status != 'not_started':
        status_labels = {
            'active': 'Активен',
            'paused': 'На паузе',
            'completed': 'Завершён',
        }
        parts.append(f"Марафон: {status_labels.get(marathon_status, marathon_status)}")

        current_topic_index = intern.get('current_topic_index', 0)
        if current_topic_index is not None:
            parts.append(f"Текущая тема: #{current_topic_index + 1}")

        completed = intern.get('completed_topics', [])
        if isinstance(completed, str):
            import json as _json
            try:
                completed = _json.loads(completed)
            except (ValueError, TypeError):
                completed = []
        if completed:
            parts.append(f"Пройдено тем: {len(completed)}")

        start_date = intern.get('marathon_start_date')
        if start_date:
            parts.append(f"Дата начала марафона: {start_date}")

    # --- Лента ---
    feed_status = intern.get('feed_status', 'not_started')
    if feed_status != 'not_started':
        feed_labels = {
            'active': 'Активна',
            'completed': 'Завершена',
        }
        parts.append(f"Лента: {feed_labels.get(feed_status, feed_status)}")

    # --- Режим ---
    mode = intern.get('mode', '')
    if mode:
        mode_labels = {'marathon': 'Марафон', 'feed': 'Лента', 'both': 'Марафон + Лента'}
        parts.append(f"Режим: {mode_labels.get(mode, mode)}")

    # --- Дата регистрации ---
    created_at = intern.get('created_at')
    if created_at:
        if hasattr(created_at, 'strftime'):
            parts.append(f"В боте с: {created_at.strftime('%Y-%m-%d')}")
        else:
            parts.append(f"В боте с: {created_at}")

    if not parts:
        return ("progress_section", "")

    header = "ПРОГРЕСС ПОЛЬЗОВАТЕЛЯ" if lang != 'en' else "USER PROGRESS"
    body = "\n".join(f"- {p}" for p in parts)
    return ("progress_section", f"\n{header}:\n{body}")


# =============================================================================
# TIER PIPELINE CONFIG
# =============================================================================

TIER_PIPELINE: Dict[int, List] = {
    1: [collect_user_profile, collect_bot_context, collect_pre_search, collect_user_progress],
    2: [collect_user_profile, collect_bot_context, collect_pre_search, collect_standard_claude, collect_iwe_instructions, collect_user_progress],
    3: [collect_user_profile, collect_bot_context, collect_pre_search, collect_standard_claude, collect_iwe_instructions, collect_personal_claude, collect_user_progress],
    4: [collect_user_profile, collect_bot_context, collect_pre_search, collect_standard_claude, collect_iwe_instructions, collect_personal_claude, collect_user_progress],
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
    ui_tier: int = -1,
    question: str = "",
) -> Dict[str, str]:
    """Запускает collectors для тира параллельно, возвращает dict placeholder → value.

    Args:
        tier: тир обслуживания (1-4)
        intern: профиль пользователя из bot DB
        lang: язык пользователя
        bot_context: self-knowledge бота
        personal_claude_md: personal CLAUDE.md из GitHub
        ui_tier: UITier (0-5) для включения в контекст (anti-hallucination)
        question: вопрос пользователя (для pre-search)

    Returns:
        Dict с ключами для fill_tier_prompt:
        {user_profile, bot_section, standard_section, iwe_section,
         personal_section, dynamic_sections, knowledge_section}
    """
    collectors = TIER_PIPELINE.get(tier, TIER_PIPELINE[1])

    # Общие kwargs для всех collectors
    ctx = dict(
        intern=intern,
        lang=lang,
        bot_context=bot_context,
        personal_claude_md=personal_claude_md,
        ui_tier=ui_tier,
        question=question,
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
        "iwe_section": "",
        "dynamic_sections": "",
        "progress_section": "",
        "knowledge_section": "",
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
