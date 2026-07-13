from __future__ import annotations

"""
Хендлеры интеграции с Digital Twin.
"""

import asyncio
import json
import logging

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command

from db.queries import get_intern
from handlers.dt_indicators import DTGroup
from helpers.typing_indicator import keep_typing
from i18n import t

logger = logging.getLogger(__name__)

twin_router = Router(name="twin")

# WP-478: отображаемые названия степеней — LMS ещё отдаёт старое имя,
# пользователь видит актуальное (DP.D.132: Первокурсник → Стажёр).
_DEGREE_DISPLAY_MAP = {
    "Первокурсник": "Стажёр",
}


def _display_degree(degree):
    """Подменяет устаревшие названия степеней из LMS на актуальные."""
    if isinstance(degree, str):
        return _DEGREE_DISPLAY_MAP.get(degree, degree)
    return degree


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
    """Формирует compact dashboard профиля Digital Twin.

    Данные: 3_derived (calculated), 2_collected (raw), 1_declarative (bot sync), indicators (Aisystant).
    """
    derived = (profile.get("_derived") or profile.get("3_derived")) or {}
    collected = profile.get("2_collected", {}) or {}

    # ── Degree ──
    degree_d = derived.get(DTGroup.DEGREE) or {}
    if degree_d.get("level"):
        degree = degree_d["level"]
    else:
        courses = (collected.get("2_2_courses", {}) or {})
        qual = (courses.get("qualification_level", {}) or {})
        degree = qual.get("level") or t('twin.lms_not_connected', lang)

    # ── Stage ──
    qualification = derived.get(DTGroup.QUALIFICATION) or {}
    stage_num = qualification.get("stage")
    if stage_num is not None:
        stage = f"{STAGE_NAMES_RU.get(stage_num, '?')} ({stage_num}/5)"
    else:
        stage = profile.get("stage") or t('twin.not_set', lang)

    # ── Agency index ──
    # IND.3.10.1 Интегральный индекс агентности
    # Писатель: profiler/scripts/dt_calc.py:calc_integral_agency_index (R28 Профилировщик, AISYS.018)
    # Триггер расчёта: dt_sync (cron 04:30 MSK) + /dt_sync on-demand. Event-driven — WP-218 Ф6 (blocked: WP-73).
    # WP-218 hotfix: /twin — stateless витрина, никаких локальных вычислений.
    agency = derived.get(DTGroup.INTEGRAL) or {}
    agency_index = agency.get("index")
    agency_text = f"{round(agency_index)}/100" if agency_index is not None else "—"

    # ── Activity from 2_4_time ──
    time_data = collected.get("2_4_time", {}) or {}
    ev_7d = time_data.get("events_last_7d", 0) or 0
    ev_30d = time_data.get("events_last_30d", 0) or 0
    active_days = time_data.get("active_days", 0) or 0

    # ── Learning from 2_2_courses + 2_3_practice ──
    courses_data = collected.get("2_2_courses", {}) or {}
    practice_data = collected.get("2_3_practice", {}) or {}
    lessons = courses_data.get("marathon_steps_total", 0) or 0
    digests = courses_data.get("feed_completed_total", 0) or 0
    training = practice_data.get("training_passed_total", 0) or 0

    # ── Coding from 2_6_coding ──
    coding = collected.get("2_6_coding", {}) or {}
    code_7d_raw = coding.get("coding_seconds_7d")
    code_30d_raw = coding.get("coding_seconds_30d")
    code_7d = round(code_7d_raw / 3600, 1) if code_7d_raw is not None else None
    code_30d = round(code_30d_raw / 3600, 1) if code_30d_raw is not None else None
    code_7d_text = f"{code_7d}ч" if code_7d is not None else "—"
    code_30d_text = f"{code_30d}ч" if code_30d is not None else "—"

    # ── Agency components (v1) ──
    components = agency.get("components") or {}
    comp_parts = []
    for key, label in [("regularity", "регулярность"), ("activity", "активность"),
                       ("learning", "развитие"), ("notifications", "уведомления"),
                       ("longevity", "длительность")]:
        val = components.get(key)
        if val is not None:
            comp_parts.append(f"{label}={round(val)}")

    # ── 3_1_agency: слоты в неделю ──
    # IND.3.1.02 Доля дней со слотом (регулярность)
    agency_group = derived.get(DTGroup.AGENCY) or {}
    slot_regularity = agency_group.get("slot_regularity")
    slot_days = agency_group.get("slot_days_per_week")

    # ── 3_2_mastery: мультипликатор ──
    # IND.3.2.04 Мультипликатор IWE (budget_hours / coding_hours)
    mastery_group = derived.get(DTGroup.MASTERY) or {}
    multiplier_today = mastery_group.get("multiplier_today")
    multiplier_7d = mastery_group.get("multiplier_7d_avg")

    # ── 3_9_it_level: IT-уровень ──
    # IND.3.9 IT competency level
    # TODO: stale key — metamodel has DTGroup.AI_USAGE ("3_9_ai_usage"), not "3_9_it_level"
    it_group = derived.get("3_9_it_level") or {}
    it_level = it_group.get("it_level")

    # ── 3_12_delivery_style: стиль доставки ──
    # IND.3.12 Preference model
    # TODO: stale key — group "3_12_delivery_style" not in metamodel (metamodel ends at 3_11_diagnostic)
    delivery_group = derived.get("3_12_delivery_style") or {}
    delivery_format = delivery_group.get("format")

    # ── 3_13_notification_resp: отклик на уведомления ──
    # IND.3.13 Notification responsiveness
    # TODO: stale key — group "3_13_notification_resp" not in metamodel
    notif_group = derived.get("3_13_notification_resp") or {}
    notif_score = notif_group.get("score")

    # ── 3_14_learning_autonomy: автономность в обучении ──
    # IND.3.14 Learning autonomy
    # TODO: stale key — group "3_14_learning_autonomy" not in metamodel
    autonomy_group = derived.get("3_14_learning_autonomy") or {}
    autonomy_score = autonomy_group.get("score")

    # ── Declarative: objective, roles ──
    indicators = profile.get("indicators", {})
    pref = indicators.get("IND.1.PREF", {}) if isinstance(indicators, dict) else {}
    pref = pref if isinstance(pref, dict) else {}
    declarative = profile.get("1_declarative", {}) or {}
    goals_sec = (declarative.get("1_2_goals", {}) or {})
    selfeval_sec = (declarative.get("1_3_selfeval", {}) or {})

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

    # ── Engine version + calculated_at (WP-218 метамодельная трассируемость) ──
    # Источник: profiler calculate_derived() → digital_twins.data['3_derived']
    # Показываем пользователю, что цифры — это снимок ЦД на момент calculated_at,
    # а не произвольное вычисление в интерфейсе.
    engine_ver = derived.get("engine_version", "")
    calc_at_iso = derived.get("calculated_at") or ""
    calc_at = calc_at_iso[:16].replace("T", " ") if calc_at_iso else ""
    engine_line = f"IND.3.10.1 · рассчитано {calc_at} · engine v{engine_ver}" if engine_ver else ""

    # ── User name ──
    name = intern.get('name', '') if intern else ''

    # ── Build text ──
    lines = [f"*{t('twin.profile_title', lang)}*"]
    if name:
        lines[0] = f"📋 *{name} — {t('twin.profile_title', lang)}*"
    lines.append("")
    lines.append(f"🎓 {t('twin.degree_label', lang)}: {_display_degree(degree)}")
    lines.append(f"⚡ {t('twin.stage_label', lang)}: {stage}")
    lines.append(f"🎯 Агентность: {agency_text}")
    lines.append("")
    lines.append(f"📊 Активность: {ev_7d} событий/7д  |  {ev_30d}/30д  |  {active_days} активных дней")
    lines.append(f"📚 Развитие: {lessons} занятий  |  {digests} дайджестов  |  {training} тренировок")
    lines.append("")

    # ── v2.0 Profiler metrics (9 new indicators) ──
    # Group 3_1: Agency structure (regularity per week)
    regularity_text = f"{round(slot_regularity, 2)}" if slot_regularity is not None else "—"
    slot_text = f"{round(slot_days)}" if slot_days is not None else "—"
    lines.append(f"⏱ Слоты: регулярность {regularity_text}  |  дн./нед: {slot_text}")

    # Group 3_2: Mastery (multipliers)
    mult_today = f"{round(multiplier_today, 2)}x" if multiplier_today is not None else "—"
    mult_7d = f"{round(multiplier_7d, 2)}x" if multiplier_7d is not None else "—"
    lines.append(f"📊 Мультипликатор: сегодня {mult_today}  |  усл. 7д {mult_7d}")

    # Group 3_9, 3_12, 3_13, 3_14: Other indicators (IT level, delivery style, notifications, autonomy)
    it_text = f"{round(it_level)}" if it_level is not None else "—"
    delivery_text = str(delivery_format).title() if delivery_format else "—"
    notif_text = f"{round(notif_score, 2)}/100" if notif_score is not None else "—"
    auto_text = f"{round(autonomy_score, 2)}/100" if autonomy_score is not None else "—"
    lines.append(f"🔧 IT: {it_text}  |  Стиль: {delivery_text}  |  Отклик: {notif_text}  |  Автономия: {auto_text}")
    lines.append("")

    lines.append(f"🎯 {t('twin.objective_label', lang)}: {objective}")
    lines.append(f"👤 {t('twin.roles_label', lang)}: {roles_text}")
    if engine_line:
        lines.append("")
        lines.append(f"_{engine_line}_")

    return "\n".join(lines)


@twin_router.message(Command("twin"))
async def cmd_twin(message: Message):
    """Команда для работы с Digital Twin."""
    from clients.gateway_mcp import gateway_mcp

    telegram_user_id = message.chat.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    text = message.text or ""
    parts = text.strip().split(maxsplit=2)
    subcommand = parts[1].lower() if len(parts) > 1 else None
    arg = parts[2] if len(parts) > 2 else None

    is_connected = gateway_mcp.is_connected(telegram_user_id)

    if subcommand == "disconnect":
        if is_connected:
            gateway_mcp.disconnect(telegram_user_id)
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
        from core.access import access_layer
        if not await access_layer.has_gateway_access(telegram_user_id):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('aisystant_sub.btn_subscribe', lang), callback_data="aisystant_subscribe")]
            ])
            await message.answer(t('twin.br_paywall', lang), parse_mode="Markdown", reply_markup=keyboard)
            return
        from clients.ory_oauth import ory_oauth
        auth_url, state = await ory_oauth.get_authorization_url(telegram_user_id)
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
        result = await gateway_mcp.dt_write("indicators.IND.1.PREF.objective", arg, telegram_user_id)
        if result:
            await message.answer(t('twin.objective_updated', lang, objective=arg), parse_mode="Markdown")
        else:
            await message.answer(t('twin.objective_error', lang))
        return

    if subcommand == "roles":
        roles = await gateway_mcp.dt_read("indicators.IND.1.PREF.role_set", telegram_user_id)
        if roles:
            roles_text = ", ".join(roles) if isinstance(roles, list) else str(roles)
            await message.answer(f"*{t('twin.roles_title', lang)}*\n{roles_text}", parse_mode="Markdown")
        else:
            await message.answer(t('twin.roles_empty', lang))
        return

    if subcommand == "degrees":
        degrees = await gateway_mcp.dt_describe("degrees", telegram_user_id)
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

    # WP-218 Ф2: единая точка чтения ЦД — gateway_mcp (→ dt-mcp → Neon по ory_id).
    # Используем get_user_profile() как в /me, чтобы избежать race-condition на _last_call_error
    profile = None
    if is_connected and gateway_mcp.is_connected(telegram_user_id):
        try:
            async with keep_typing(message):
                profile = await gateway_mcp.get_user_profile(telegram_user_id)
        except Exception as e:
            logger.error(f"[/twin] get_user_profile raised for {telegram_user_id}: {type(e).__name__}: {e}", exc_info=True)
            await message.answer(t('twin.temporary_error', lang))
            return

    # WP-218 Ф2: fallback на обычный engagement view если ЦД пуст
    if profile is None or not profile:
        fallback_data = await _fallback_engagement(telegram_user_id)
        if fallback_data:
            # Переформатируем fallback (flat structure) в структуру для _profile_text
            profile = {
                "2_collected": fallback_data,
                "_derived": fallback_data.get("_derived", {})
            }
        else:
            profile = None

    if profile is None:
        # Пользователь не подключен или ЦД пуст → показать ошибку с кнопкой переподключения
        if not is_connected:
            from clients.ory_oauth import ory_oauth
            auth_url, _state = await ory_oauth.get_authorization_url(telegram_user_id)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t('twin.btn_connect', lang), url=auth_url)]
            ])
            await message.answer(
                t('twin.token_expired', lang),
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        else:
            await message.answer(t('twin.empty_profile', lang))
        return

    # WP-218 Ф2: единая точка чтения ЦД — gateway_mcp (→ dt-mcp → Neon по ory_id).
    # Убран повторный enrich через get_engagement_data() — он ходил по user_uuid
    # из development.engagement, который для некоторых chat_id возвращает ДРУГОЙ
    # UUID (дубль в engagement view) и перезаписывал корректный _derived на
    # устаревшие данные. Принцип №4: stateless-интерфейсы — читаем ровно один раз.

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t('twin.btn_insights', lang), callback_data="twin_insights"),
            InlineKeyboardButton(text=t('twin.btn_degrees', lang), callback_data="twin_degrees"),
        ],
        [
            InlineKeyboardButton(text=t('twin.btn_disconnect', lang), callback_data="twin_disconnect"),
        ],
    ])

    try:
        text = _profile_text(profile, lang, intern=intern)
    except Exception as e:
        logger.error(f"[/twin] _profile_text raised for {telegram_user_id}: {type(e).__name__}: {e}", exc_info=True)
        await message.answer(t('twin.temporary_error', lang))
        return
    try:
        await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        logger.warning(f"[/twin] Markdown parse failed for {telegram_user_id}: {e}; fallback plain")
        try:
            await message.answer(text, reply_markup=keyboard)
        except Exception as e2:
            logger.error(f"[/twin] plain fallback also failed for {telegram_user_id}: {type(e2).__name__}: {e2}", exc_info=True)



@twin_router.callback_query(F.data == "twin_degrees")
async def callback_twin_degrees(callback: CallbackQuery):
    from clients.gateway_mcp import gateway_mcp

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not gateway_mcp.is_connected(telegram_user_id):
        await callback.answer(t('twin.not_connected_alert', lang), show_alert=True)
        return

    await callback.answer()

    degrees = await gateway_mcp.dt_describe("degrees", telegram_user_id)
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
    from clients.gateway_mcp import gateway_mcp

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    if not gateway_mcp.is_connected(telegram_user_id):
        await callback.answer(t('twin.already_disconnected', lang), show_alert=True)
        return

    gateway_mcp.disconnect(telegram_user_id)
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

STAGE_EMOJI = {1: "🌱", 2: "🌿", 3: "🌳", 4: "⚡", 5: "🌟"}
STAGE_NAMES_RU = {
    1: "Случайный", 2: "Практикующий", 3: "Систематический",
    4: "Дисциплинированный", 5: "Проактивный",
}


def _build_me_dashboard(engagement: dict, intern: dict, lang: str,
                        dt_profile: dict = None) -> str:
    """Compact dashboard из 2_collected + 3_derived + DT profile."""
    name = (intern or {}).get('name', '') or 'Участник'

    courses = engagement.get('2_2_courses') or {}
    practice = engagement.get('2_3_practice') or {}
    time_data = engagement.get('2_4_time') or {}
    coding = engagement.get('2_6_coding') or {}
    iwe = engagement.get('2_7_iwe') or {}
    derived = engagement.get('_derived') or {}

    lines = [f"📋 *{name} — {t('twin.profile_title', lang)}*"]
    lines.append("")

    # Degree (from DT profile or derived)
    degree = None
    degree_d = derived.get('3_8_degree') or {}
    if degree_d.get('level'):
        degree = degree_d['level']
    elif dt_profile:
        dt_degree = ((dt_profile.get('3_derived') or {}).get('3_8_degree') or {})
        if dt_degree.get('level'):
            degree = dt_degree['level']
        else:
            dt_courses = ((dt_profile.get('2_collected') or {}).get('2_2_courses') or {})
            qual = dt_courses.get('qualification_level') or {}
            degree = qual.get('level')
    if degree:
        degree = _display_degree(degree)
        lines.append(f"🎓 {t('twin.degree_label', lang)}: {degree}")

    # Stage
    qualification = derived.get('3_4_qualification') or {}
    stage_num = qualification.get('stage')
    if stage_num is not None:
        stage = f"{STAGE_NAMES_RU.get(stage_num, '?')} ({stage_num}/5)"
        lines.append(f"⚡ {t('twin.stage_label', lang)}: {stage}")

    # Agency index
    # IND.3.10.1 Интегральный индекс агентности
    # Писатель: profiler/scripts/dt_calc.py:calc_integral_agency_index (WP-218)
    integral = derived.get('3_10_integral') or {}
    agency_index = integral.get('index')
    if agency_index is not None:
        lines.append(f"🎯 Агентность: {round(agency_index)}/100")

    lines.append("")

    # Activity
    events_7d = time_data.get('events_last_7d', 0) or 0
    events_30d = time_data.get('events_last_30d', 0) or 0
    active_days = time_data.get('active_days', 0) or 0
    lines.append(f"📊 Активность: {events_7d} событий/7д  |  {events_30d}/30д  |  {active_days} активных дней")

    # Learning
    marathon = courses.get('marathon_steps_total', 0) or 0
    feed = courses.get('feed_completed_total', 0) or 0
    training = practice.get('training_passed_total', 0) or 0
    lines.append(f"📚 Развитие: {marathon} занятий  |  {feed} дайджестов  |  {training} тренировок")

    # Coding (WakaTime)
    if coding and coding.get('coding_seconds_7d', 0):
        hrs_7d = coding['coding_seconds_7d'] / 3600
        hrs_30d = (coding.get('coding_seconds_30d', 0) or 0) / 3600
        lines.append(f"💻 Код: {hrs_7d:.1f}ч/7д  |  {hrs_30d:.1f}ч/30д")

    # IWE (git)
    if iwe and iwe.get('commits_7d', 0):
        lines.append(
            f"🔧 IWE: {iwe['commits_7d']} коммитов/7д"
            f"  |  РП: {iwe.get('wp_completed_total', 0)} done"
            f"  |  {iwe.get('wp_in_progress_count', 0)} in progress"
        )

    # Notifications
    notifications = engagement.get('2_5_notifications') or {}
    notif_30d = notifications.get('notifications_30d', 0) or 0
    if notif_30d:
        lines.append(f"📬 Уведомления: {notif_30d}/30д")

    lines.append("")

    # Objective + Roles (from DT profile)
    objective = None
    roles_text = None
    if dt_profile:
        indicators = dt_profile.get('indicators', {})
        pref = indicators.get('IND.1.PREF', {}) if isinstance(indicators, dict) else {}
        declarative = dt_profile.get('1_declarative', {}) or {}
        goals_sec = declarative.get('1_2_goals', {}) or {}
        selfeval_sec = declarative.get('1_3_selfeval', {}) or {}
        objective = pref.get('objective') or goals_sec.get('09_Цели обучения')
        roles_raw = pref.get('role_set') or selfeval_sec.get('06_Роли')
        if isinstance(roles_raw, list):
            roles_text = ', '.join(roles_raw)
        elif isinstance(roles_raw, str):
            roles_text = roles_raw
    if not objective:
        objective = (intern or {}).get('goals')
    if not roles_text:
        roles_text = (intern or {}).get('role')

    if objective:
        lines.append(f"🎯 {t('twin.objective_label', lang)}: {objective}")
    if roles_text:
        lines.append(f"👤 {t('twin.roles_label', lang)}: {roles_text}")

    # ── v2.0 Profiler metrics (9 new indicators) ──
    # IND.3.1: Agency structure
    agency_group = derived.get('3_1_agency') or {}
    slot_regularity = agency_group.get('slot_regularity')
    slot_days = agency_group.get('slot_days_per_week')
    if any(v is not None for v in (slot_regularity, slot_days)):
        regularity_t = f"{round(slot_regularity, 2)}" if slot_regularity is not None else "—"
        slot_t = f"{round(slot_days)}" if slot_days is not None else "—"
        lines.append(f"⏱ Слоты: регулярность {regularity_t}  |  дн./нед: {slot_t}")

    # IND.3.2: Mastery
    mastery_group = derived.get('3_2_mastery') or {}
    multiplier_today = mastery_group.get('multiplier_today')
    multiplier_7d = mastery_group.get('multiplier_7d_avg')
    if any(v is not None for v in (multiplier_today, multiplier_7d)):
        mult_today_t = f"{round(multiplier_today, 2)}x" if multiplier_today is not None else "—"
        mult_7d_t = f"{round(multiplier_7d, 2)}x" if multiplier_7d is not None else "—"
        lines.append(f"📊 Мультипликатор: сегодня {mult_today_t}  |  усл. 7д {mult_7d_t}")

    # TODO: stale key — metamodel has DTGroup.AI_USAGE ("3_9_ai_usage"), not "3_9_it_level"
    it_group = derived.get('3_9_it_level') or {}
    it_level = it_group.get('it_level')
    # TODO: stale key — group "3_12_delivery_style" not in metamodel (metamodel ends at 3_11_diagnostic)
    delivery_group = derived.get('3_12_delivery_style') or {}
    delivery_format = delivery_group.get('format')
    # TODO: stale key — group "3_13_notification_resp" not in metamodel
    notif_group = derived.get('3_13_notification_resp') or {}
    notif_score = notif_group.get('score')
    # TODO: stale key — group "3_14_learning_autonomy" not in metamodel
    autonomy_group = derived.get('3_14_learning_autonomy') or {}
    autonomy_score = autonomy_group.get('score')
    if any(v is not None for v in (it_level, delivery_format, notif_score, autonomy_score)):
        it_t = f"{round(it_level)}" if it_level is not None else "—"
        delivery_t = str(delivery_format).title() if delivery_format else "—"
        notif_t = f"{round(notif_score, 2)}/100" if notif_score is not None else "—"
        auto_t = f"{round(autonomy_score, 2)}/100" if autonomy_score is not None else "—"
        lines.append(f"🔧 IT: {it_t}  |  Стиль: {delivery_t}  |  Отклик: {notif_t}  |  Автономия: {auto_t}")

    # Agency components
    components = integral.get('components') or {}
    if components:
        comp_parts = []
        for key, label in [("regularity", "регулярность"), ("activity", "активность"),
                           ("learning", "развитие"), ("notifications", "уведомления"),
                           ("longevity", "стаж")]:
            val = components.get(key)
            if val is not None:
                comp_parts.append(f"{label}={round(val)}")
        if comp_parts:
            lines.append(f"\n🧩 Компоненты: {', '.join(comp_parts)}")

    # Engine version
    engine_v = derived.get('engine_version', '')
    calc_at = derived.get('calculated_at', '')[:10] if derived.get('calculated_at') else ''
    if engine_v:
        lines.append(f"\n_engine v{engine_v} | {calc_at}_")

    return '\n'.join(lines)


async def _fallback_engagement(chat_id: int) -> dict | None:
    """Fallback: сырые метрики из development.engagement + профиль из indicators.digital_twins.

    WP-218 Ф2 + WP-269: бот — pure reader. Источники по слоям:
    - Сырые метрики (2_collected): development.engagement view (platform DB)
    - account_id (canonical UUID): persona.ory_identity WHERE telegram_id (persona DB)
    - 3_derived + 2_6/2_7: indicators.digital_twins (новая БД, не platform!)

    Использует persona.ory_identity как source of truth для UUID — исключает дрейф
    development.engagement.user_uuid, который мог содержать устаревший дублирующий UUID.
    """
    from db.connection import get_pool

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

        # WP-269: canonical account_id из persona.ory_identity (source of truth для UUID)
        account_id = None
        try:
            from db.connection import get_persona_pool
            persona_pool = await get_persona_pool()
            async with persona_pool.acquire() as persona_conn:
                account_id = await persona_conn.fetchval(
                    "SELECT account_id FROM ory_identity WHERE telegram_id = $1",
                    chat_id,
                )
        except Exception as pe:
            logger.warning(f"[/me] Persona account_id lookup failed: {pe}")

        # Merge 2_6/2_7 и 3_derived из indicators.digital_twins (WP-269: новая БД)
        if account_id:
            try:
                from db.connection import get_indicators_pool
                ind_pool = await get_indicators_pool()
                async with ind_pool.acquire() as ind_conn:
                    existing = await ind_conn.fetchval(
                        "SELECT data->'2_collected' FROM digital_twins WHERE user_id = $1",
                        str(account_id),
                    )
                    if existing:
                        existing_c = json.loads(existing) if isinstance(existing, str) else existing
                        for key in ('2_6_coding', '2_7_iwe'):
                            if key in existing_c and key not in collected:
                                collected[key] = existing_c[key]

                    # F1: предпочитаем calculated_profile.indicators->'3_derived'
                    # (F2 dual-write заполняет с 4 мая; fallback на digital_twins)
                    cp_indicators = await ind_conn.fetchval(
                        "SELECT indicators FROM public.calculated_profile WHERE account_id = $1::uuid",
                        str(account_id),
                    )
                    if cp_indicators:
                        cp_data = json.loads(cp_indicators) if isinstance(cp_indicators, str) else cp_indicators
                        derived = cp_data.get('3_derived')
                        if derived:
                            collected['_derived'] = derived

                    # Fallback: legacy digital_twins (пока calculated_profile не заполнен)
                    if '_derived' not in collected:
                        existing_derived = await ind_conn.fetchval(
                            "SELECT data->'3_derived' FROM digital_twins WHERE user_id = $1",
                            str(account_id),
                        )
                        if existing_derived:
                            derived = json.loads(existing_derived) if isinstance(existing_derived, str) else existing_derived
                            collected['_derived'] = derived
            except Exception as dt_e:
                logger.warning(f"[/me] Fallback digital_twins read failed: {dt_e}")

        return collected
    except Exception as e:
        logger.warning(f"[/me] Fallback engagement failed: {e}")
        return None



def _build_me_t2_dashboard(intern: dict, activity: dict, learning: dict) -> str:
    """Dashboard для T2 (подписка, без ЦД): активность + обучение."""
    name = (intern or {}).get('name', '') or 'Участник'
    lines = [f"📋 *{name} — Мой прогресс*\n"]

    # Activity
    total = activity.get('total', 0) or 0
    streak = activity.get('streak', 0) or 0
    week = activity.get('days_active_this_week', 0) or 0
    lines.append(f"📊 *Активность:* {week} дней на неделе  |  {total} всего")
    if streak > 1:
        lines.append(f"🔥 *Серия:* {streak} дней  |  Рекорд: {activity.get('longest_streak', 0) or 0}")

    # Learning
    lessons = learning.get('theory_answers', 0) or 0
    tasks = learning.get('work_products', 0) or 0
    digests = learning.get('digests', 0) or 0
    fixations = learning.get('fixations', 0) or 0
    if lessons or tasks or digests:
        parts = []
        if lessons:
            parts.append(f"{lessons} занятий")
        if digests:
            parts.append(f"{digests} дайджестов")
        if tasks:
            parts.append(f"{tasks} заданий")
        lines.append(f"\n📚 *Развитие:* {' | '.join(parts)}")

    return '\n'.join(lines)


def _build_me_keyboard(tier: int) -> InlineKeyboardMarkup:
    """Inline-кнопки /me по тиру (WP-160 Ф1)."""
    from core.tier_config import UITier

    rows = []
    # Подробнее + Мои данные — для всех тиров
    row1 = [
        InlineKeyboardButton(text="📊 Подробнее", callback_data="me_detailed"),
        InlineKeyboardButton(text="🗑 Мои данные", callback_data="me_mydata"),
    ]
    rows.append(row1)

    # AI-анализ + Профиль ЦД — только T3+
    if tier >= UITier.T3_PERSONALIZATION:
        row2 = [
            InlineKeyboardButton(text="🔍 AI-анализ", callback_data="twin_insights"),
            InlineKeyboardButton(text="👤 Профиль ЦД", callback_data="twin_profile"),
        ]
        rows.append(row2)

    return InlineKeyboardMarkup(inline_keyboard=rows)


@twin_router.message(Command("me"))
async def cmd_me(message: Message):
    """Команда /me — tier-aware дашборд (WP-160 Ф1)."""
    from db.queries.events import log_event
    from db.queries.activity import get_activity_stats
    from db.queries.answers import get_weekly_marathon_stats
    from db.queries.answers import get_weekly_feed_stats
    from core.tier_detector import detect_ui_tier
    from core.tier_config import UITier

    telegram_user_id = message.chat.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)
    tier = await detect_ui_tier(telegram_user_id)

    # WP-218 Ф2: единая точка чтения — Gateway MCP (→ dt-mcp → Neon по ory_id).
    # T3+ path: полный ЦД-dashboard
    if tier >= UITier.T3_PERSONALIZATION:
        # WP-406 Ф20: consent gap. A user can reach T3 by connecting an AI client via
        # claude.ai OAuth or /test — paths that never prompt tracking consent (it lives
        # in the /link onboarding flow). Without opt-in the profiler skips their events,
        # so the profile stays empty. Detect the gap and route to the existing,
        # idempotent consent opt-in screen instead of showing an empty dashboard.
        # Fail-open: on any lookup error, fall through to the normal dashboard.
        try:
            from db.queries.consent import get_consent
            from db.queries.identity import get_user_uuid
            _uuid = await get_user_uuid(telegram_user_id)
            if _uuid:
                _consent = await get_consent(str(_uuid))
                if not _consent or not _consent["opt_in"]:
                    from handlers.consent import show_consent_optin
                    await show_consent_optin(message)
                    return
        except Exception as e:
            # Fail-open осознан и прошёл Security Gate B7.3 §Б (commit 7f08bd2, WP-406 Ф20,
            # 2026-07-01): новых PII-логов нет, переиспользован провалидированный consent-flow.
            # error, не warning: get_user_uuid/get_consent возвращают None на штатных случаях
            # (нет строки), исключение здесь означает реальный инфраструктурный сбой — должен
            # быть виден в error_logs/RUNBOOK (core/error_handler.py ловит только ERROR+).
            logger.error(f"[/me] consent-gap check failed for {telegram_user_id}: {e}")

        dt_profile = None
        engagement = None
        try:
            from clients.gateway_mcp import gateway_mcp
            if gateway_mcp.is_connected(telegram_user_id):
                dt_profile = await gateway_mcp.get_user_profile(telegram_user_id)
                if dt_profile:
                    # Разбираем полный data на 2_collected + _derived
                    collected = dt_profile.get('2_collected') or {}
                    derived = dt_profile.get('3_derived') or {}
                    if collected:
                        engagement = dict(collected)
                        if derived:
                            engagement['_derived'] = derived
        except Exception as e:
            logger.warning(f"[/me] Gateway profile failed for {telegram_user_id}: {e}")

        # Fallback для отсутствующих/пустых ЦД — читаем сырое engagement view
        if not engagement:
            engagement = await _fallback_engagement(telegram_user_id)

        if engagement:
            dashboard = _build_me_dashboard(engagement, intern, lang, dt_profile=dt_profile)
            keyboard = _build_me_keyboard(tier)
            try:
                await message.answer(dashboard, parse_mode="Markdown", reply_markup=keyboard)
            except Exception:
                await message.answer(dashboard, reply_markup=keyboard)
            # Audit trail
            try:
                derived = engagement.get('_derived') or {}
                await log_event(
                    user_id=telegram_user_id,
                    event_type='dt_view_requested',
                    payload={
                        'source': 'bot', 'command': '/me',
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
            return

    # T0-T2 path: базовый dashboard из user_state (параллельная загрузка)
    activity, learning, feed = await asyncio.gather(
        get_activity_stats(telegram_user_id),
        get_weekly_marathon_stats(telegram_user_id),
        get_weekly_feed_stats(telegram_user_id),
        return_exceptions=False
    )

    # Merge feed stats into learning dict
    learning['digests'] = feed.get('digests', 0) if feed else 0
    learning['fixations'] = feed.get('fixations', 0) if feed else 0

    name = (intern or {}).get('name', '') or 'Участник'

    if tier >= UITier.T2_LEARNING:
        # T2: активность + обучение
        dashboard = _build_me_t2_dashboard(intern, activity, learning)
    else:
        # T0-T1: только активность
        lines = [f"📋 *{name} — Мой прогресс*\n"]
        total = activity.get('total', 0) or 0
        streak = activity.get('streak', 0) or 0
        week = activity.get('days_active_this_week', 0) or 0
        lines.append(f"📊 *Активность:* {week} дней на неделе  |  {total} всего")
        if streak > 1:
            lines.append(f"🔥 *Серия:* {streak} дней  |  Рекорд: {activity.get('longest_streak', 0) or 0}")
        if not total and not streak:
            lines.append("\n💡 Начните с /learn или /feed, чтобы увидеть свой прогресс.")
        dashboard = '\n'.join(lines)

    keyboard = _build_me_keyboard(tier)
    try:
        await message.answer(dashboard, parse_mode="Markdown", reply_markup=keyboard)
    except Exception:
        await message.answer(dashboard, reply_markup=keyboard)


@twin_router.callback_query(F.data == "me_detailed")
async def callback_me_detailed(callback: CallbackQuery):
    """Drill-down: подробная статистика (redirect на /progress SM)."""
    await callback.answer()
    from handlers.progress import cmd_progress
    await cmd_progress(callback.message, state=None)


@twin_router.callback_query(F.data == "me_mydata")
async def callback_me_mydata(callback: CallbackQuery):
    """Drill-down: мои данные (redirect на /mydata SM)."""
    await callback.answer()
    from handlers import get_dispatcher
    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    dispatcher = get_dispatcher()
    if dispatcher and dispatcher.is_sm_active:
        await dispatcher.route_command('mydata', intern)
    else:
        await callback.message.answer("Используйте /mydata для просмотра и управления данными.")


@twin_router.callback_query(F.data == "twin_profile")
async def callback_twin_profile(callback: CallbackQuery):
    """Переход к профилю ЦД из /me."""
    from clients.gateway_mcp import gateway_mcp

    telegram_user_id = callback.from_user.id
    intern = await get_intern(telegram_user_id)
    lang = _lang(intern)

    await callback.answer()
    profile = await gateway_mcp.get_user_profile(telegram_user_id)
    if profile:
        text = _profile_text(profile, lang, intern=intern)
        try:
            await callback.message.answer(text, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"[twin_profile cb] Markdown parse failed for {telegram_user_id}: {e}; fallback plain")
            await callback.message.answer(text)
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

    await message.answer(t('twin.insights_loading', lang))

    # WP-218 Ф2: единая точка чтения — Gateway MCP (→ dt-mcp → Neon по ory_id).
    engagement = None
    try:
        from clients.gateway_mcp import gateway_mcp
        profile = await gateway_mcp.get_user_profile(telegram_user_id)
        if profile:
            collected = profile.get('2_collected') or {}
            if collected:
                engagement = dict(collected)
                derived = profile.get('3_derived') or {}
                if derived:
                    engagement['_derived'] = derived
    except Exception as e:
        logger.warning(f"[Twin Insights] Gateway profile failed: {e}")

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
    telegram_user_id = message.chat.id

    await message.answer(t('twin.insights_detailed_loading', lang))

    # WP-218 Ф2: единая точка чтения — Gateway MCP (→ dt-mcp → Neon по ory_id).
    engagement = None
    try:
        from clients.gateway_mcp import gateway_mcp
        profile = await gateway_mcp.get_user_profile(telegram_user_id)
        if profile:
            collected = profile.get('2_collected') or {}
            if collected:
                engagement = dict(collected)
                derived = profile.get('3_derived') or {}
                if derived:
                    engagement['_derived'] = derived
    except Exception as e:
        logger.warning(f"[Twin Insights Detailed] Gateway profile failed: {e}")

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
