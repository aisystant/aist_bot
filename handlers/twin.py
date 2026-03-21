"""
Хендлеры интеграции с Digital Twin.
"""

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


async def _handle_insights(message: Message, intern: dict, lang: str):
    """Генерирует AI-интерпретацию engagement данных из ЦД (Phase 5A)."""
    from db.queries.dt_sync import get_engagement_data
    from db.queries.identity import get_user_uuid

    telegram_user_id = message.chat.id

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
        "Example: uptime=8 means scheduler ran on 8 different days, not that system was set up 8 days ago\n\n"
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
