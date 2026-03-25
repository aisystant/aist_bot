"""
Хендлеры интеграции с Digital Twin.
"""

import json
import logging

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command

from db.queries import get_intern
from helpers.typing_indicator import keep_typing
from i18n import t

logger = logging.getLogger(__name__)

twin_router = Router(name="twin")


def _format_degrees(raw: str) -> str:
    """Конвертирует markdown-таблицу степеней в читаемый формат для Telegram."""
    lines = raw.strip().split('\n')
    result_parts = []
    for line in lines:
        line = line.strip()
        # Пропускаем разделитель таблицы и заголовок колонок
        if line.startswith('|--') or line.startswith('| Code'):
            continue
        # Строка таблицы → форматируем
        if line.startswith('|') and line.endswith('|'):
            cells = [c.strip() for c in line.strip('|').split('|')]
            if len(cells) >= 4:
                code, order, name, desc = cells[0], cells[1], cells[2], cells[3]
                result_parts.append(f"*{order}. {name}*\n{desc}")
            continue
        # Заголовок — берём только русское название
        if line.startswith('# '):
            continue
        if 'Степени квалификации' in line and not line.startswith('|'):
            result_parts.insert(0, f"*{line}*")
            continue
        # Пропускаем Version и пустые строки
        if line.startswith('Version:') or not line:
            continue
    return '\n\n'.join(result_parts)


def _lang(intern) -> str:
    if not intern:
        return 'ru'
    return intern.get('language', 'ru') or 'ru'


def _profile_text(profile: dict, lang: str, intern: dict = None) -> str:
    """Формирует текст профиля Digital Twin.

    Fallback chain: indicators.IND.1.PREF (Aisystant) → 1_declarative (bot sync) → intern (bot DB).
    """
    degree = profile.get("degree", t('twin.not_set', lang))
    stage = profile.get("stage", t('twin.not_set', lang))

    # Source 1: indicators path (Aisystant platform writes here)
    indicators = profile.get("indicators", {})
    pref = indicators.get("IND.1.PREF", {}) if isinstance(indicators, dict) else {}
    pref = pref if isinstance(pref, dict) else {}

    # Source 2: declarative path (bot sync writes here)
    declarative = profile.get("1_declarative", {}) if isinstance(profile, dict) else {}
    goals_sec = (declarative.get("1_2_goals", {}) if isinstance(declarative, dict) else {}) or {}
    selfeval_sec = (declarative.get("1_3_selfeval", {}) if isinstance(declarative, dict) else {}) or {}

    # Merge with fallback chain
    objective = (
        pref.get("objective")
        or goals_sec.get("09_Цели обучения")
        or (intern.get('goals') if intern else None)
        or t('twin.not_set', lang)
    )

    roles_raw = (
        pref.get("role_set")
        or selfeval_sec.get("06_Роли")
        or (intern.get('role') if intern else None)
    )
    if isinstance(roles_raw, str):
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
    elif isinstance(roles_raw, list):
        roles = roles_raw
    else:
        roles = []
    roles_text = ", ".join(roles) if roles else t('twin.not_set_plural', lang)

    time_budget = (
        pref.get("weekly_time_budget")
        or t('twin.not_set_m', lang)
    )

    return (
        f"*{t('twin.profile_title', lang)}*\n\n"
        f"*{t('twin.degree_label', lang)}:* {degree}\n"
        f"*{t('twin.stage_label', lang)}:* {stage}\n"
        f"*{t('twin.objective_label', lang)}:* {objective}\n"
        f"*{t('twin.roles_label', lang)}:* {roles_text}\n"
        f"*{t('twin.time_budget_label', lang)}:* {time_budget} {t('twin.hours_per_week', lang)}"
    )


@twin_router.message(Command("twin"))
async def cmd_twin(message: Message):
    """Команда для работы с Digital Twin."""
    from clients.digital_twin import digital_twin

    telegram_user_id = message.chat.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    text = message.text or ""
    parts = text.strip().split(maxsplit=2)
    subcommand = parts[1].lower() if len(parts) > 1 else None
    arg = parts[2] if len(parts) > 2 else None

    is_connected = digital_twin.is_connected(telegram_user_id)

    if subcommand == "disconnect":
        if is_connected:
            digital_twin.disconnect(telegram_user_id)
            # Clear persistent flag
            try:
                from db import get_pool
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute('UPDATE public.users SET dt_connected_at = NULL WHERE telegram_id = $1', telegram_user_id)
            except Exception:
                pass
            await message.answer(t('twin.disconnected', lang))
        else:
            await message.answer(t('twin.not_connected', lang))
        return

    if not is_connected:
        auth_url, state = digital_twin.get_authorization_url(telegram_user_id)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t('twin.btn_connect', lang), url=auth_url)]
        ])
        await message.answer(
            f"*{t('twin.connect_title', lang)}*\n\n"
            f"{t('twin.connect_desc', lang)}",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    if subcommand == "objective" and arg:
        await message.answer(t('twin.saving_objective', lang))
        result = await digital_twin.set_learning_objective(telegram_user_id, arg)
        if result:
            await message.answer(t('twin.objective_updated', lang, objective=arg), parse_mode="Markdown")
        else:
            await message.answer(t('twin.objective_error', lang))
        return

    if subcommand == "roles":
        roles = await digital_twin.get_roles(telegram_user_id)
        if roles:
            roles_text = ", ".join(roles) if isinstance(roles, list) else str(roles)
            await message.answer(f"*{t('twin.roles_title', lang)}*\n{roles_text}", parse_mode="Markdown")
        else:
            await message.answer(t('twin.roles_empty', lang))
        return

    if subcommand == "degrees":
        degrees = await digital_twin.get_degrees(telegram_user_id)
        if degrees:
            text = _format_degrees(degrees if isinstance(degrees, str) else str(degrees))
            if len(text) > 4000:
                text = text[:4000] + "\n..."
            await message.answer(text, parse_mode="Markdown")
        else:
            await message.answer(t('twin.degrees_error', lang))
        return

    if subcommand == "insights":
        await _handle_insights(message, intern, lang)
        return

    # По умолчанию: показать профиль
    await message.answer(t('twin.loading_profile', lang))
    async with keep_typing(message):
        profile = await digital_twin.get_user_profile(telegram_user_id)

    if profile is None:
        await message.answer(t('twin.unavailable', lang))
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t('twin.btn_insights', lang), callback_data="twin_insights"),
            InlineKeyboardButton(text=t('twin.btn_degrees', lang), callback_data="twin_degrees"),
        ],
        [
            InlineKeyboardButton(text=t('twin.btn_disconnect', lang), callback_data="twin_disconnect"),
        ],
    ])

    await message.answer(_profile_text(profile, lang, intern=intern), parse_mode="Markdown", reply_markup=keyboard)



@twin_router.callback_query(F.data == "twin_degrees")
async def callback_twin_degrees(callback: CallbackQuery):
    from clients.digital_twin import digital_twin

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not digital_twin.is_connected(telegram_user_id):
        await callback.answer(t('twin.not_connected_alert', lang), show_alert=True)
        return

    await callback.answer()

    degrees = await digital_twin.get_degrees(telegram_user_id)
    if degrees:
        text = _format_degrees(degrees if isinstance(degrees, str) else str(degrees))
        if len(text) > 4000:
            text = text[:4000] + "\n..."
        await callback.message.answer(text, parse_mode="Markdown")
    else:
        await callback.message.answer(t('twin.degrees_error', lang))


@twin_router.callback_query(F.data == "twin_insights")
async def callback_twin_insights(callback: CallbackQuery):
    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    await callback.answer()
    await _handle_insights(callback.message, intern, lang)


@twin_router.callback_query(F.data == "twin_insights_detailed")
async def callback_twin_insights_detailed(callback: CallbackQuery):
    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    await callback.answer()
    await _handle_insights_detailed(callback.message, intern, lang)


@twin_router.callback_query(F.data == "twin_disconnect")
async def callback_twin_disconnect(callback: CallbackQuery):
    from clients.digital_twin import digital_twin

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not digital_twin.is_connected(telegram_user_id):
        await callback.answer(t('twin.already_disconnected', lang), show_alert=True)
        return

    digital_twin.disconnect(telegram_user_id)
    # Clear persistent flag
    try:
        from db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute('UPDATE public.users SET dt_connected_at = NULL WHERE telegram_id = $1', telegram_user_id)
    except Exception:
        pass
    await callback.answer(t('twin.disconnected_alert', lang), show_alert=True)
    await callback.message.edit_text(
        t('twin.disconnected_desc', lang),
        parse_mode="Markdown"
    )


def _annotate_repos(repos_list: list) -> str:
    """Annotate repo names with their type for LLM context."""
    _REPO_TYPES = {
        'DS-my-strategy': 'governance (plans, reviews, personal strategy)',
        'DS-ecosystem-development': 'governance (coordination, processes)',
        'DS-IT-systems': 'instrument (bots, MCP servers, tools)',
        'DS-ai-systems': 'instrument (AI system descriptions)',
        'DS-principles-curriculum': 'surface (courses, curriculum)',
        'DS-Knowledge-Index': 'instrument (knowledge index)',
    }
    _REPO_PREFIXES = {
        'PACK-': 'pack (domain knowledge, source-of-truth)',
        'FMT-': 'template (reusable formats)',
        'SPF': 'base (second-level framework)',
        'FPF': 'base (first principles framework)',
        'ZP': 'base (zero principles)',
    }

    if not repos_list or not isinstance(repos_list, list):
        return 'N/A'

    parts = []
    for item in repos_list[:10]:
        if isinstance(item, dict):
            name = item.get('name', '?')
            commits = item.get('commits', 0)
            seconds = item.get('seconds', 0)
        else:
            name = str(item)
            commits, seconds = 0, 0

        # Determine type
        repo_type = _REPO_TYPES.get(name, '')
        if not repo_type:
            for prefix, rtype in _REPO_PREFIXES.items():
                if name.startswith(prefix):
                    repo_type = rtype
                    break

        label = f" [{repo_type}]" if repo_type else ""
        if commits:
            parts.append(f"{name}{label}: {commits} commits")
        elif seconds:
            hrs = seconds / 3600
            parts.append(f"{name}{label}: {hrs:.1f}h")
        else:
            parts.append(f"{name}{label}")

    return '; '.join(parts)


# ═══════════════════════════════════════════════════════════
# /me — Compact Dashboard (WP-135 Ф0)
# ═══════════════════════════════════════════════════════════

STAGE_EMOJI = {0: "🌱", 1: "🌿", 2: "🌳", 3: "⚡", 4: "🌟"}
STAGE_NAMES_RU = {
    0: "Случайный", 1: "Практикующий", 2: "Систематический",
    3: "Дисциплинированный", 4: "Проактивный",
}


def _build_me_dashboard(engagement: dict, intern: dict, lang: str) -> str:
    """Compact dashboard (10-15 строк) из 2_collected + 3_derived."""
    name = (intern or {}).get('name', '') or 'Участник'

    account = engagement.get('2_1_account') or {}
    courses = engagement.get('2_2_courses') or {}
    practice = engagement.get('2_3_practice') or {}
    time_data = engagement.get('2_4_time') or {}
    notifications = engagement.get('2_5_notifications') or {}
    coding = engagement.get('2_6_coding') or {}
    iwe = engagement.get('2_7_iwe') or {}
    derived = engagement.get('_derived') or {}

    lines = [f"📋 *{name} — Мой ЦД*\n"]

    # Stage + Agency Index
    qualification = derived.get('3_4_qualification') or {}
    integral = derived.get('3_10_integral') or {}
    stage = qualification.get('stage', 0)
    stage_emoji = STAGE_EMOJI.get(stage, "🌱")
    agency_index = integral.get('index', 0)

    if qualification:
        path = qualification.get('path', 'learner')
        path_label = " 🔧" if path == "builder" else ""
        lines.append(
            f"{stage_emoji} *Ступень:* {STAGE_NAMES_RU.get(stage, '?')} ({stage}/4){path_label}"
            f"  |  🎯 *Агентность:* {agency_index}/100"
        )
    else:
        lines.append("⚠️ Derived-индикаторы ещё не рассчитаны (sync 04:30)")

    # Activity
    events_7d = time_data.get('events_last_7d', 0) or 0
    events_30d = time_data.get('events_last_30d', 0) or 0
    active_days = time_data.get('active_days', 0) or 0
    sessions = account.get('sessions_total', 0) or 0
    lines.append(
        f"\n📊 *Активность:* {events_7d} событий/7д"
        f"  |  {events_30d}/30д  |  {active_days} активных дней"
    )

    # Learning
    marathon = courses.get('marathon_steps_total', 0) or 0
    feed = courses.get('feed_completed_total', 0) or 0
    training = practice.get('training_passed_total', 0) or 0
    if marathon or feed or training:
        lines.append(
            f"📚 *Обучение:* {marathon} уроков"
            f"  |  {feed} дайджестов  |  {training} тренировок пройдено"
        )

    # Coding (WakaTime)
    if coding and coding.get('coding_seconds_7d', 0):
        hrs_7d = coding['coding_seconds_7d'] / 3600
        hrs_30d = (coding.get('coding_seconds_30d', 0) or 0) / 3600
        lines.append(f"💻 *Код:* {hrs_7d:.1f}ч/7д  |  {hrs_30d:.1f}ч/30д")

    # IWE (git)
    if iwe and iwe.get('commits_7d', 0):
        lines.append(
            f"🔧 *IWE:* {iwe['commits_7d']} коммитов/7д"
            f"  |  РП: {iwe.get('wp_completed_total', 0)} done"
            f"  |  {iwe.get('wp_in_progress_count', 0)} in progress"
        )

    # Notifications
    notif_30d = notifications.get('notifications_30d', 0) or 0
    if notif_30d:
        lines.append(f"📬 *Уведомления:* {notif_30d}/30д")

    # Agency components
    components = integral.get('components') or {}
    if components:
        lines.append(
            f"\n🧩 *Компоненты агентности:*"
            f" рег.={components.get('regularity', 0):.0f}"
            f" акт.={components.get('activity', 0):.0f}"
            f" обуч.={components.get('learning', 0):.0f}"
            f" увед.={components.get('notifications', 0):.0f}"
            f" стаж={components.get('longevity', 0):.0f}"
        )

    # Engine version
    engine_v = derived.get('engine_version', '')
    calc_at = derived.get('calculated_at', '')[:10] if derived.get('calculated_at') else ''
    if engine_v:
        lines.append(f"\n_engine v{engine_v} | {calc_at}_")

    return '\n'.join(lines)


async def _fallback_engagement(chat_id: int) -> dict | None:
    """Fallback: читаем development.engagement напрямую (без digital_twins).

    Используется когда sync ещё не запускался (pilot, новые пользователи).
    Вычисляет 3_derived на лету.
    """
    from db.connection import get_pool
    from db.queries.dt_calc import calculate_derived

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow('''
                SELECT
                    sessions_total, ai_chats_total,
                    marathon_steps_total, marathon_tasks_total,
                    feed_completed_total,
                    training_attempts_total, training_passed_total,
                    assessments_total, events_total,
                    first_event_at, last_event_at,
                    active_days, events_last_7d, events_last_30d
                FROM development.engagement
                WHERE user_id = $1
            ''', chat_id)

            if not row:
                return None

            def _ts(v):
                return v.isoformat() if v else None

            collected = {
                "2_1_account": {
                    "sessions_total": row['sessions_total'],
                    "events_total": row['events_total'],
                    "first_event_at": _ts(row['first_event_at']),
                    "last_event_at": _ts(row['last_event_at']),
                },
                "2_2_courses": {
                    "marathon_steps_total": row['marathon_steps_total'],
                    "feed_completed_total": row['feed_completed_total'],
                },
                "2_3_practice": {
                    "training_attempts_total": row['training_attempts_total'],
                    "training_passed_total": row['training_passed_total'],
                    "assessments_total": row['assessments_total'],
                    "marathon_tasks_total": row['marathon_tasks_total'],
                },
                "2_4_time": {
                    "active_days": row['active_days'],
                    "events_last_7d": row['events_last_7d'],
                    "events_last_30d": row['events_last_30d'],
                    "ai_chats_total": row['ai_chats_total'],
                },
            }

            # Merge existing 2_6/2_7 if available (WP-174 builder path)
            user_uuid_row = await conn.fetchval(
                "SELECT user_uuid FROM development.engagement WHERE user_id = $1",
                chat_id,
            )
            if user_uuid_row:
                existing = await conn.fetchval(
                    "SELECT data->'2_collected' FROM digital_twins WHERE user_id = $1",
                    str(user_uuid_row),
                )
                if existing:
                    existing_c = json.loads(existing) if isinstance(existing, str) else existing
                    for key in ('2_6_coding', '2_7_iwe'):
                        if key in existing_c and key not in collected:
                            collected[key] = existing_c[key]

            # Derive on-the-fly
            derived = calculate_derived(collected)
            if derived:
                collected['_derived'] = derived

            return collected
    except Exception as e:
        logger.warning(f"[/me] Fallback engagement failed: {e}")
        return None


@twin_router.message(Command("me"))
async def cmd_me(message: Message):
    """Команда /me — компактный дашборд ЦД (WP-135 Ф0)."""
    from db.queries.dt_sync import get_engagement_data
    from db.queries.identity import get_user_uuid
    from db.queries.events import log_event

    telegram_user_id = message.chat.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    user_uuid = await get_user_uuid(telegram_user_id)
    if not user_uuid:
        await message.answer(
            "⚠️ Данные ЦД недоступны. Используйте /twin для подключения."
        )
        return

    engagement = await get_engagement_data(str(user_uuid))

    # Fallback: если digital_twins пуста → читаем engagement view напрямую
    if not engagement:
        engagement = await _fallback_engagement(telegram_user_id)

    if not engagement:
        await message.answer(
            "📊 Данных об активности пока нет. Начните с /learn или /feed."
        )
        return

    dashboard = _build_me_dashboard(engagement, intern, lang)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔍 AI-анализ",
                callback_data="twin_insights",
            ),
            InlineKeyboardButton(
                text="👤 Профиль ЦД",
                callback_data="twin_profile",
            ),
        ],
    ])

    try:
        await message.answer(dashboard, parse_mode="Markdown", reply_markup=keyboard)
    except Exception:
        await message.answer(dashboard, reply_markup=keyboard)

    # Audit trail (WP-135)
    try:
        account = engagement.get('2_1_account') or {}
        derived = engagement.get('_derived') or {}
        await log_event(
            user_id=telegram_user_id,
            event_type='dt_view_requested',
            payload={
                'source': 'bot',
                'command': '/me',
                'snapshot': {
                    'active_days': (engagement.get('2_4_time') or {}).get('active_days', 0),
                    'events_7d': (engagement.get('2_4_time') or {}).get('events_last_7d', 0),
                    'stage': (derived.get('3_4_qualification') or {}).get('stage'),
                    'agency_index': (derived.get('3_10_integral') or {}).get('index'),
                },
            },
            source='bot',
        )
    except Exception as e:
        logger.debug(f"[/me] Audit trail failed: {e}")


@twin_router.callback_query(F.data == "twin_profile")
async def callback_twin_profile(callback: CallbackQuery):
    """Переход к профилю ЦД из /me."""
    from clients.digital_twin import digital_twin

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    await callback.answer()
    profile = await digital_twin.get_user_profile(telegram_user_id)
    if profile:
        await callback.message.answer(
            _profile_text(profile, lang, intern=intern),
            parse_mode="Markdown",
        )
    else:
        await callback.message.answer("⚠️ Профиль ЦД недоступен.")


# Shared IWE platform context for insights prompts
_IWE_PLATFORM_CONTEXT = (
    "\n\nPLATFORM CONTEXT:\n"
    "User builds IWE — personal development platform. All repos = ONE ecosystem.\n"
    "Repo types: PACK-* = domain knowledge (source-of-truth, ALREADY documented), "
    "DS-my-strategy = governance (plans/reviews, high commits = daily rituals, NORMAL), "
    "FMT-* = templates, DS-IT-systems = instruments.\n"
    "WakaTime CAVEATS: 'Other' language = Claude Code CLI (normal). "
    "'Github'/'IWE' project = umbrella. Per-repo hours are UNRELIABLE — "
    "Claude Code work is attributed to umbrella, not individual repos. "
    "NEVER cite per-repo WakaTime hours or call repos 'neglected'.\n"
    "Zero bot-training = normal (learns through practice, not bot courses).\n\n"
    "FORBIDDEN in recommendations:\n"
    "- Creating docs/Design Documents/README (exist in Pack)\n"
    "- Startup/investor/team advice (personal platform)\n"
    "- Citing WakaTime per-repo hours as evidence\n"
    "- Flagging zero bot-training as problem\n"
    "- 'Pack Knowledge Audit' or similar (Pack is maintained via Claude Code)\n\n"
    "GOOD recommendations: daily rhythm consistency, WP completion rate, "
    "skill depth in a specific area, test coverage, code quality practices\n"
)


import time as _time

# In-memory cache for insights: {telegram_id: (timestamp, html_result)}
_insights_cache: dict[int, tuple[float, str]] = {}
_INSIGHTS_CACHE_TTL = 1800  # 30 minutes
_INSIGHTS_CACHE_MAX_SIZE = 500


def _cleanup_insights_cache() -> None:
    """Remove expired entries and enforce max size."""
    now = _time.time()
    expired = [k for k, (ts, _) in _insights_cache.items() if now - ts >= _INSIGHTS_CACHE_TTL]
    for k in expired:
        del _insights_cache[k]
    if len(_insights_cache) > _INSIGHTS_CACHE_MAX_SIZE:
        sorted_keys = sorted(_insights_cache, key=lambda k: _insights_cache[k][0])
        for k in sorted_keys[:len(_insights_cache) - _INSIGHTS_CACHE_MAX_SIZE]:
            del _insights_cache[k]


async def _handle_insights(message: Message, intern: dict, lang: str):
    """Генерирует AI-интерпретацию engagement данных из ЦД (Phase 5A)."""
    from db.queries.dt_sync import get_engagement_data
    from db.queries.identity import get_user_uuid

    telegram_user_id = message.chat.id

    # Cleanup expired/oversized cache entries
    _cleanup_insights_cache()

    # Check cache
    cached = _insights_cache.get(telegram_user_id)
    if cached:
        cached_at, cached_result = cached
        if _time.time() - cached_at < _INSIGHTS_CACHE_TTL:
            logger.info(f"[Twin Insights] Cache hit for {telegram_user_id}")
            from helpers.message_split import prepare_html_parts
            parts = prepare_html_parts(cached_result)
            detail_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t('twin.btn_insights_detailed', lang),
                    callback_data="twin_insights_detailed",
                )]
            ])
            for i, part in enumerate(parts):
                is_last = (i == len(parts) - 1)
                try:
                    await message.answer(
                        part, parse_mode="HTML",
                        reply_markup=detail_kb if is_last else None,
                    )
                except Exception:
                    await message.answer(
                        part,
                        reply_markup=detail_kb if is_last else None,
                    )
            return
        else:
            del _insights_cache[telegram_user_id]

    # digital_twins.user_id = public.users.id (bot UUID, NOT dt_tokens.dt_user_id)
    user_uuid = await get_user_uuid(telegram_user_id)
    if not user_uuid:
        await message.answer(t('twin.insights_no_dt', lang))
        return

    await message.answer(t('twin.insights_loading', lang))

    engagement = await get_engagement_data(str(user_uuid))
    if not engagement:
        await message.answer(t('twin.insights_no_data', lang))
        return

    # Собрать контекст для промпта
    name = (intern or {}).get('name', '')
    goals = (intern or {}).get('goals', '')
    occupation = (intern or {}).get('occupation', '')

    account = engagement.get('2_1_account', {})
    courses = engagement.get('2_2_courses', {})
    practice = engagement.get('2_3_practice', {})
    time_data = engagement.get('2_4_time', {})
    coding = engagement.get('2_6_coding', {})
    iwe = engagement.get('2_7_iwe', {})

    data_summary = (
        f"[BOT ACTIVITY — only interactions with this Telegram bot]\n"
        f"Bot sessions: {account.get('sessions_total', 0)}, "
        f"Bot events: {account.get('events_total', 0)}, "
        f"First bot activity: {account.get('first_event_at', 'N/A')}, "
        f"Last bot activity: {account.get('last_event_at', 'N/A')}\n"
        f"Marathon steps: {courses.get('marathon_steps_total', 0)}, "
        f"Feed digests: {courses.get('feed_completed_total', 0)}\n"
        f"Training attempts: {practice.get('training_attempts_total', 0)}, "
        f"Passed: {practice.get('training_passed_total', 0)}, "
        f"Assessments: {practice.get('assessments_total', 0)}, "
        f"Marathon tasks: {practice.get('marathon_tasks_total', 0)}\n"
        f"Bot active days: {time_data.get('active_days', 0)} (bot only, not total), "
        f"Bot events last 7d: {time_data.get('events_last_7d', 0)}, "
        f"Bot events last 30d: {time_data.get('events_last_30d', 0)}, "
        f"AI chats in bot: {time_data.get('ai_chats_total', 0)}"
    )

    # Notifications (WP-152 Ф4 — IND.2.5)
    notifications = engagement.get('2_5_notifications') or {}
    if notifications.get('notifications_total', 0):
        data_summary += (
            f"\n[NOTIFICATIONS]\n"
            f"Total: {notifications.get('notifications_total', 0)}, "
            f"7d: {notifications.get('notifications_7d', 0)}, "
            f"30d: {notifications.get('notifications_30d', 0)}\n"
            f"Types: lessons={notifications.get('lesson_notifications', 0)}, "
            f"reminders={notifications.get('reminder_notifications', 0)}, "
            f"nudges={notifications.get('nudge_notifications', 0)}, "
            f"feed={notifications.get('feed_digest_notifications', 0)}, "
            f"milestones={notifications.get('milestone_notifications', 0)}"
        )

    # Derived indicators (WP-151 Ф4 — IND.3)
    derived = engagement.get('_derived', {})
    if derived:
        qualification = derived.get('3_4_qualification', {})
        integral = derived.get('3_10_integral', {})
        agency = derived.get('3_1_agency', {})
        data_summary += (
            f"\n[DERIVED INDICATORS (calculated by engine v{derived.get('engine_version', '?')})]\n"
            f"Student stage: {qualification.get('stage', '?')}/4 "
            f"({qualification.get('stage_name_ru', 'N/A')}, "
            f"path={qualification.get('path', 'learner')})\n"
            f"Agency index: {integral.get('index', 0)}/100 "
            f"(regularity={integral.get('components', {}).get('regularity', 0)}, "
            f"activity={integral.get('components', {}).get('activity', 0)}, "
            f"learning={integral.get('components', {}).get('learning', 0)})\n"
            f"Slot regularity: {agency.get('slot_days_per_week', 0)} days/week"
        )

    # Coding activity (WakaTime — IND.2.6)
    if coding:
        today_min = coding.get('coding_seconds_today', 0) // 60
        week_hrs = coding.get('coding_seconds_7d', 0) / 3600
        month_hrs = coding.get('coding_seconds_30d', 0) / 3600
        data_summary += (
            f"\nCoding today: {today_min} min, "
            f"7d: {week_hrs:.1f} hrs, "
            f"30d: {month_hrs:.1f} hrs, "
            f"Active days (30d): {coding.get('coding_active_days_30d', 0)}\n"
            f"Top languages: {coding.get('top_languages', 'N/A')}\n"
            f"Top projects (with repo types): {_annotate_repos(coding.get('top_projects', []))}\n"
            f"Top editors: {coding.get('top_editors', 'N/A')}"
        )

    # IWE activity (git, sessions, WPs — IND.2.7)
    if iwe:
        data_summary += (
            f"\nGit commits today: {iwe.get('commits_today', 0)}, "
            f"7d: {iwe.get('commits_7d', 0)}, "
            f"30d: {iwe.get('commits_30d', 0)}\n"
            f"Active repos (7d, with types): {_annotate_repos(iwe.get('repos_active_7d', []))}\n"
            f"Files changed (7d): {iwe.get('files_changed_7d', 0)}, "
            f"Lines +{iwe.get('lines_added_7d', 0)} / -{iwe.get('lines_removed_7d', 0)}\n"
            f"Claude sessions (7d): {iwe.get('claude_sessions_7d', 0)}, "
            f"total: {iwe.get('claude_sessions_total', 0)}\n"
            f"WPs completed: {iwe.get('wp_completed_total', 0)}, "
            f"in progress: {iwe.get('wp_in_progress_count', 0)}\n"
            f"Scheduler health: {iwe.get('scheduler_health', 'N/A')}, "
            f"Exocortex uptime: {iwe.get('exocortex_uptime_days', 0)} days"
        )

    lang_instruction = "Отвечай на русском." if lang == 'ru' else f"Answer in {lang}."

    system_prompt = (
        "You are a personal learning advisor analyzing a student's Digital Twin data. "
        "Give a brief, encouraging activity summary and ONE specific actionable recommendation. "
        "Be warm but honest. Use standard Markdown formatting (**bold**, *italic*). "
        "Use ## for section titles. "
        "Use emojis for visual structure: ✅ for achievements, ⚠️ for attention points, "
        "📊 for data highlights, 🎯 for recommendations, 💡 for tips. "
        f"Keep response under 350 words. Complete the recommendation fully — do not cut off mid-sentence. {lang_instruction}\n\n"
        "DATA DICTIONARY (interpret numbers correctly):\n"
        "- 'Sessions/Events/Active days/AI chats' = activity IN THIS BOT only, NOT total activity\n"
        "- 'Coding today/7d/30d' = WakaTime tracked coding time (all editors, all projects)\n"
        "- 'Coding active days (30d)' = days with ANY coding activity in last 30 days\n"
        "- 'Git commits' = commits across ALL IWE repos (one ecosystem, not separate projects)\n"
        "- 'Active repos (7d)' = repos within ONE IWE workspace (Pack, DS, FMT are modules, not separate projects)\n"
        "- 'Claude sessions' = Claude Code AI-assisted coding sessions\n"
        "- 'WPs' = Work Products (managed deliverables with deadlines)\n"
        "- 'Exocortex uptime' = number of days with recorded scheduler activity (NOT 'launched N days ago'). "
        "Example: uptime=8 means scheduler ran on 8 different days, not that system was set up 8 days ago\n"
        "- 'Student stage' (0-4) = calculated learning maturity: 0=Random, 1=Practicing, 2=Systematic, "
        "3=Disciplined, 4=Proactive. Based on regularity, sessions, training\n"
        "- 'Agency index' (0-100) = weighted aggregate: regularity(30%), activity(25%), learning(25%), "
        "notifications(10%), longevity(10%)\n"
        "- 'Slot regularity' = average days/week with learning activity\n"
        "- Notifications = bot-sent messages (lessons, reminders, nudges, feed digests, milestones)\n\n"
        "RULES:\n"
        "- Do NOT confuse bot active days with total activity — coding/git data shows the full picture\n"
        "- Do NOT flag normal coding amounts as burnout risk\n"
        "- Multiple IWE repos = one ecosystem, not fragmentation\n"
        "- Focus on the balance between theory (courses, training) and practice (coding, commits)\n"
        "- If coding or IWE data is present, it shows the MAIN activity — bot data is supplementary\n"
        "- EVERY number MUST include its time period explicitly: '4 активных дня за последние 30 дней', "
        "NOT just '4 дня'. '285 коммитов за неделю', NOT '285 коммитов'. No bare numbers.\n"
        "- If WPs completed = 0 AND WPs in progress = 0, say 'данные о рабочих продуктах пока не подключены к ЦД' — "
        "do NOT invent or guess WP counts from other metrics. Only report WP numbers when they are non-zero in the data.\n"
        "- If derived indicators are present, use student_stage and agency_index to frame recommendations "
        "— match advice to the user's current stage (don't recommend Proactive practices to a Random user)\n"
        "- Write in natural Russian. Do NOT use English words unless they are established "
        "platform terms (WP, Claude Code, WakaTime, Exocortex, Pack). "
        "For example: 'критерии завершения', NOT 'Definition of Done'; "
        "'кодовая база', NOT 'codebase'; 'обзор кода', NOT 'code review'."
        + _IWE_PLATFORM_CONTEXT
    )

    user_prompt = (
        f"Student: {name}\n"
        f"{'Occupation: ' + occupation if occupation else ''}\n"
        f"{'Learning goals: ' + goals if goals else ''}\n\n"
        f"Engagement data:\n{data_summary}\n\n"
        "Analyze this data and provide:\n"
        f"1. Brief activity summary (title it '## Анализ активности {name}'). "
        "What's going well, what needs attention.\n"
        "2. One specific recommendation for the next step"
    )

    try:
        from bot import claude
        from config import CLAUDE_MODEL_SONNET

        async with keep_typing(message):
            result = await claude.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=1200,
                model=CLAUDE_MODEL_SONNET,
            )

        if result:
            # Store in cache
            _insights_cache[telegram_user_id] = (_time.time(), result)
            from helpers.message_split import prepare_html_parts
            parts = prepare_html_parts(result)
            detail_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=t('twin.btn_insights_detailed', lang),
                    callback_data="twin_insights_detailed",
                )]
            ])
            for i, part in enumerate(parts):
                is_last = (i == len(parts) - 1)
                try:
                    await message.answer(
                        part, parse_mode="HTML",
                        reply_markup=detail_kb if is_last else None,
                    )
                except Exception:
                    await message.answer(
                        part,
                        reply_markup=detail_kb if is_last else None,
                    )
        else:
            await message.answer(t('twin.insights_error', lang))
    except Exception as e:
        logger.error(f"[Twin Insights] Failed: {e}")
        await message.answer(t('twin.insights_error', lang))


async def _handle_insights_detailed(message: Message, intern: dict, lang: str):
    """Расширенный AI-анализ engagement данных — детальный разбор по каждой метрике."""
    from db.queries.dt_sync import get_engagement_data
    from db.queries.identity import get_user_uuid

    telegram_user_id = message.chat.id

    user_uuid = await get_user_uuid(telegram_user_id)
    if not user_uuid:
        await message.answer(t('twin.insights_no_dt', lang))
        return

    await message.answer(t('twin.insights_detailed_loading', lang))

    engagement = await get_engagement_data(str(user_uuid))
    if not engagement:
        await message.answer(t('twin.insights_no_data', lang))
        return

    name = (intern or {}).get('name', '')
    goals = (intern or {}).get('goals', '')
    occupation = (intern or {}).get('occupation', '')

    account = engagement.get('2_1_account', {})
    courses = engagement.get('2_2_courses', {})
    practice = engagement.get('2_3_practice', {})
    time_data = engagement.get('2_4_time', {})
    coding = engagement.get('2_6_coding', {})
    iwe = engagement.get('2_7_iwe', {})

    data_summary = (
        f"[BOT ACTIVITY — only interactions with this Telegram bot]\n"
        f"Bot sessions: {account.get('sessions_total', 0)}, "
        f"Bot events: {account.get('events_total', 0)}, "
        f"First bot activity: {account.get('first_event_at', 'N/A')}, "
        f"Last bot activity: {account.get('last_event_at', 'N/A')}\n"
        f"Marathon steps: {courses.get('marathon_steps_total', 0)}, "
        f"Feed digests: {courses.get('feed_completed_total', 0)}\n"
        f"Training attempts: {practice.get('training_attempts_total', 0)}, "
        f"Passed: {practice.get('training_passed_total', 0)}, "
        f"Assessments: {practice.get('assessments_total', 0)}, "
        f"Marathon tasks: {practice.get('marathon_tasks_total', 0)}\n"
        f"Bot active days: {time_data.get('active_days', 0)} (bot only, not total), "
        f"Bot events last 7d: {time_data.get('events_last_7d', 0)}, "
        f"Bot events last 30d: {time_data.get('events_last_30d', 0)}, "
        f"AI chats in bot: {time_data.get('ai_chats_total', 0)}"
    )

    if coding:
        today_min = coding.get('coding_seconds_today', 0) // 60
        week_hrs = coding.get('coding_seconds_7d', 0) / 3600
        month_hrs = coding.get('coding_seconds_30d', 0) / 3600
        data_summary += (
            f"\nCoding today: {today_min} min, "
            f"7d: {week_hrs:.1f} hrs, "
            f"30d: {month_hrs:.1f} hrs, "
            f"Active days (30d): {coding.get('coding_active_days_30d', 0)}\n"
            f"Top languages: {coding.get('top_languages', 'N/A')}\n"
            f"Top projects (with repo types): {_annotate_repos(coding.get('top_projects', []))}\n"
            f"Top editors: {coding.get('top_editors', 'N/A')}"
        )

    if iwe:
        data_summary += (
            f"\nGit commits today: {iwe.get('commits_today', 0)}, "
            f"7d: {iwe.get('commits_7d', 0)}, "
            f"30d: {iwe.get('commits_30d', 0)}\n"
            f"Active repos (7d, with types): {_annotate_repos(iwe.get('repos_active_7d', []))}\n"
            f"Files changed (7d): {iwe.get('files_changed_7d', 0)}, "
            f"Lines +{iwe.get('lines_added_7d', 0)} / -{iwe.get('lines_removed_7d', 0)}\n"
            f"Claude sessions (7d): {iwe.get('claude_sessions_7d', 0)}, "
            f"total: {iwe.get('claude_sessions_total', 0)}\n"
            f"WPs completed: {iwe.get('wp_completed_total', 0)}, "
            f"in progress: {iwe.get('wp_in_progress_count', 0)}\n"
            f"Scheduler health: {iwe.get('scheduler_health', 'N/A')}, "
            f"Exocortex uptime: {iwe.get('exocortex_uptime_days', 0)} days"
        )

    lang_instruction = "Отвечай на русском." if lang == 'ru' else f"Answer in {lang}."

    system_prompt = (
        "You are a personal learning advisor providing a DETAILED analysis of a student's Digital Twin data. "
        "Go through EACH metric group separately with specific numbers and interpretation. "
        "Use standard Markdown formatting (**bold**, *italic*). "
        "Use ## for section titles and ### for subsections. "
        "Use emojis for visual structure: ✅ for achievements, ⚠️ for attention points, "
        "📊 for data highlights, 🎯 for recommendations, 💡 for tips, 🔍 for deep dives. "
        f"Be thorough — 500-800 words. {lang_instruction}\n\n"
        "DATA DICTIONARY (interpret numbers correctly):\n"
        "- 'Sessions/Events/Active days/AI chats' = activity IN THIS BOT only, NOT total activity\n"
        "- 'Coding today/7d/30d' = WakaTime tracked coding time (all editors, all projects)\n"
        "- 'Coding active days (30d)' = days with ANY coding activity in last 30 days\n"
        "- 'Git commits' = commits across ALL IWE repos (one ecosystem, not separate projects)\n"
        "- 'Active repos (7d)' = repos within ONE IWE workspace (Pack, DS, FMT are modules, not separate projects)\n"
        "- 'Claude sessions' = Claude Code AI-assisted coding sessions\n"
        "- 'WPs' = Work Products (managed deliverables with deadlines)\n"
        "- 'Exocortex uptime' = number of days with recorded scheduler activity (NOT 'launched N days ago'). "
        "Example: uptime=8 means scheduler ran on 8 different days, not that system was set up 8 days ago\n\n"
        "RULES:\n"
        "- Do NOT confuse bot active days with total activity — coding/git data shows the full picture\n"
        "- Do NOT flag normal coding amounts as burnout risk\n"
        "- Multiple IWE repos = one ecosystem, not fragmentation\n"
        "- Analyze EACH group: bot activity, learning (courses+practice), coding, IWE ecosystem\n"
        "- Compare 7d vs 30d trends where data allows\n"
        "- Give 2-3 specific, actionable recommendations at the end\n"
        "- EVERY number MUST include its time period explicitly: '4 активных дня за последние 30 дней', "
        "NOT just '4 дня'. '285 коммитов за неделю', NOT '285 коммитов'. No bare numbers.\n"
        "- If WPs completed = 0 AND WPs in progress = 0, say 'данные о рабочих продуктах пока не подключены к ЦД' — "
        "do NOT invent or guess WP counts from other metrics. Only report WP numbers when they are non-zero in the data.\n"
        "- Write in natural Russian. Do NOT use English words unless they are established "
        "platform terms (WP, Claude Code, WakaTime, Exocortex, Pack). "
        "For example: 'критерии завершения', NOT 'Definition of Done'; "
        "'кодовая база', NOT 'codebase'; 'обзор кода', NOT 'code review'."
        + _IWE_PLATFORM_CONTEXT
    )

    user_prompt = (
        f"Student: {name}\n"
        f"{'Occupation: ' + occupation if occupation else ''}\n"
        f"{'Learning goals: ' + goals if goals else ''}\n\n"
        f"Engagement data:\n{data_summary}\n\n"
        "Provide a DETAILED analysis with:\n"
        f"1. Title: '## Детальный анализ активности {name}'\n"
        "2. Separate subsection for each data group (### Бот, ### Обучение, ### Кодирование, ### IWE экосистема)\n"
        "3. For each group: key numbers, trends, interpretation\n"
        "4. Overall assessment: balance between theory and practice\n"
        "5. 2-3 specific recommendations with concrete next steps"
    )

    try:
        from bot import claude
        from config import CLAUDE_MODEL_SONNET

        async with keep_typing(message):
            result = await claude.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=1500,
                model=CLAUDE_MODEL_SONNET,
            )

        if result:
            from helpers.message_split import prepare_html_parts
            parts = prepare_html_parts(result)
            for part in parts:
                try:
                    await message.answer(part, parse_mode="HTML")
                except Exception:
                    await message.answer(part)
        else:
            await message.answer(t('twin.insights_error', lang))
    except Exception as e:
        logger.error(f"[Twin Insights Detailed] Failed: {e}")
        await message.answer(t('twin.insights_error', lang))
