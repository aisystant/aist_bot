from __future__ import annotations

"""
Engagement Analyzer — threshold rules для автоматических nudges.

WP-85 Phase 5C (MVP) + WP-117 Ф2/Ф3 (расширение).

Анализирует проекции 2_collected + 3_derived из digital_twins JSONB.
Возвращает список nudge-рекомендаций для каждого пользователя.

Принцип: данные без интерпретации — зеркало. Nudge превращает зеркало в наставника.
"""

import logging
from datetime import datetime, timezone

from config import MOSCOW_TZ
from core.nudge_policy import stopgap_suppression_reason

logger = logging.getLogger(__name__)


def _moscow_today():
    """«Сегодня» в МСК-календаре — тот же календарь, в котором пишутся last_active_date/last_slot_at."""
    return datetime.now(MOSCOW_TZ).date()


# ═══════════════════════════════════════════════════════════
# THRESHOLD RULES
# ═══════════════════════════════════════════════════════════

# Each rule: (rule_id, check_fn, cooldown_days)
# check_fn(engagement, user_meta) -> nudge_key | None
# cooldown_days: minimum days between same nudge type for a user

RULES = []


def rule(rule_id, cooldown_days=7):
    """Decorator to register a threshold rule."""
    def decorator(fn):
        RULES.append((rule_id, fn, cooldown_days))
        return fn
    return decorator


@rule("slot_missing_3d", cooldown_days=7)
def check_slot_missing_3d(engagement, user_meta):
    """Пользователь не записывал /slot 3+ дня. WP-117 Этап 1."""
    last_slot = user_meta.get('last_slot_date')
    if not last_slot:
        return None  # никогда не логировал — не нудить

    if hasattr(last_slot, 'date'):
        # last_slot_at — TIMESTAMPTZ (UTC). Переводим в МСК-дату перед сравнением,
        # иначе активность около полуночи МСК считается «вчера» (WP-117 Ф-roles, off-by-one).
        if last_slot.tzinfo is None:
            last_slot = last_slot.replace(tzinfo=timezone.utc)
        last_slot = last_slot.astimezone(MOSCOW_TZ).date()
    elif isinstance(last_slot, str):
        try:
            last_slot = datetime.fromisoformat(last_slot).date()
        except ValueError:
            return None

    days_since = (_moscow_today() - last_slot).days
    if days_since >= 3:
        return "nudge_slot_missing_3d"
    return None


@rule("inactivity_3d", cooldown_days=7)
def check_inactivity_3d(engagement, user_meta):
    """Пользователь не взаимодействовал с ботом 3+ дня."""
    last_active = user_meta.get('last_active_date')
    if not last_active:
        return None

    if isinstance(last_active, str):
        try:
            last_active = datetime.fromisoformat(last_active).date()
        except ValueError:
            return None

    days_inactive = (_moscow_today() - last_active).days
    if days_inactive >= 3:
        return "nudge_inactivity"
    return None


@rule("streak_drop", cooldown_days=14)
def check_streak_drop(engagement, user_meta):
    """Стрик активности упал (был >= 3, стал 0)."""
    longest = user_meta.get('longest_streak', 0) or 0
    current = user_meta.get('active_days_streak', 0) or 0

    if longest >= 3 and current == 0:
        return "nudge_streak_drop"
    return None


@rule("low_engagement_7d", cooldown_days=14)
def check_low_engagement_7d(engagement, user_meta):
    """Очень низкая активность за 7 дней (< 2 событий)."""
    time_data = engagement.get('2_4_time', {})
    events_7d = time_data.get('events_last_7d', 0) or 0

    if events_7d < 2:
        return "nudge_low_engagement"
    return None


@rule("marathon_stalled", cooldown_days=7)
def check_marathon_stalled(engagement, user_meta):
    """Марафон активен, но нет прогресса за 3+ дня."""
    marathon_status = user_meta.get('marathon_status')
    if marathon_status != 'active':
        return None

    last_active = user_meta.get('last_active_date')
    if not last_active:
        return None

    if isinstance(last_active, str):
        try:
            last_active = datetime.fromisoformat(last_active).date()
        except ValueError:
            return None

    # last_active_date пишется через moscow_today() (МСК-календарь) — сравниваем
    # с МСК-«сегодня», иначе окно 00:00-03:00 МСК даёт off-by-one (WP-117 Ф-roles,
    # инцидент 21 апр: активность в 00:06 МСК ошибочно считалась «вчера» по UTC).
    days_inactive = (_moscow_today() - last_active).days
    if days_inactive >= 3:
        return "nudge_marathon_stalled"
    return None


@rule("achievement_sessions", cooldown_days=30)
def check_sessions_milestone(engagement, user_meta):
    """Достижение: 10/25/50/100 сессий."""
    account = engagement.get('2_1_account', {})
    sessions = account.get('sessions_total', 0) or 0

    milestones = [100, 50, 25, 10]
    for m in milestones:
        if sessions >= m:
            # Only trigger at threshold (not already notified)
            return f"nudge_sessions_{m}"
    return None


@rule("achievement_active_days", cooldown_days=30)
def check_active_days_milestone(engagement, user_meta):
    """Достижение: 7/14/30 активных дней."""
    time_data = engagement.get('2_4_time', {})
    active_days = time_data.get('active_days', 0) or 0

    milestones = [30, 14, 7]
    for m in milestones:
        if active_days >= m:
            return f"nudge_active_days_{m}"
    return None


# ═══════════════════════════════════════════════════════════
# DERIVED-AWARE RULES (WP-117 Ф3, dep: WP-151 Ф4)
# ═══════════════════════════════════════════════════════════

# Each derived_rule: (rule_id, check_fn, cooldown_days)
# check_fn(engagement, user_meta, derived) -> nudge_key | None

DERIVED_RULES = []


def derived_rule(rule_id, cooldown_days=7):
    """Decorator to register a derived-aware rule."""
    def decorator(fn):
        DERIVED_RULES.append((rule_id, fn, cooldown_days))
        return fn
    return decorator


@derived_rule("stage_upgrade", cooldown_days=30)
def check_stage_upgrade(engagement, user_meta, derived):
    """Пользователь достиг новой ступени — поздравление + рекомендации потока и тира (WP-349 Ф3).

    Payload расширен: recommend_stream (S{N}), suggest_tier_upgrade (если нет подписки).
    Бот читает payload и рисует соответствующие inline-кнопки через handlers/tier_upgrade.py.
    """
    qualification = derived.get('3_4_qualification') or {}
    stage = qualification.get('stage', 1)

    if stage < 2:
        return None

    # has_subscription из account или из meta (может быть заполнен разными writers)
    has_subscription = (
        user_meta.get('has_subscription') or
        (derived.get('3_1_account') or {}).get('has_subscription', False)
    )

    nudge_type = f"nudge_stage_reached_{stage}"
    # suggest_tier_upgrade: True если пилот на T1 (нет подписки) — открываем T2
    suggest_tier_upgrade = not has_subscription and stage >= 2
    recommend_stream = f"S{stage}"

    return {
        "nudge_type": nudge_type,
        "stage": stage,
        "recommend_stream": recommend_stream,
        "suggest_tier_upgrade": suggest_tier_upgrade,
    }


@derived_rule("agency_growing", cooldown_days=14)
def check_agency_growing(engagement, user_meta, derived):
    """Индекс агентности растёт — поддержать прогресс."""
    integral = derived.get('3_10_integral') or {}
    index = integral.get('index', 0) or 0

    if 40 <= index < 70:
        return "nudge_agency_growing"
    return None


@derived_rule("agency_high", cooldown_days=30)
def check_agency_high(engagement, user_meta, derived):
    """Высокий индекс агентности — мотивировать на следующий уровень."""
    integral = derived.get('3_10_integral') or {}
    index = integral.get('index', 0) or 0

    if index >= 70:
        return "nudge_agency_high"
    return None


@derived_rule("low_regularity", cooldown_days=14)
def check_low_regularity(engagement, user_meta, derived):
    """Низкая регулярность — предложить перестроить расписание."""
    agency = derived.get('3_1_agency') or {}
    days_per_week = agency.get('slot_days_per_week', 0) or 0

    # Practicing+ but low regularity (< 2 days/week)
    qualification = derived.get('3_4_qualification') or {}
    stage = qualification.get('stage', 1)

    if stage >= 1 and days_per_week < 2:
        return "nudge_low_regularity"
    return None


@derived_rule("notification_fatigue", cooldown_days=30)
def check_notification_fatigue(engagement, user_meta, derived):
    """Много уведомлений, мало реакции — снизить частоту."""
    notifications = (engagement or {}).get('2_5_notifications') or {}
    notif_30d = notifications.get('notifications_30d', 0) or 0

    time_data = (engagement or {}).get('2_4_time') or {}
    events_30d = time_data.get('events_last_30d', 0) or 0

    # High notifications, low activity = fatigue
    if notif_30d > 20 and events_30d < 5:
        return "nudge_reduce_frequency"
    return None


@derived_rule("onboarder_gap", cooldown_days=7)
def check_onboarder_gap(engagement, user_meta, derived):
    """Разрыв Онбордера (Х2/Х3) не закрыт 3+ дня — вернуть в поток «Освоиться» (WP-406/WP-117 Ф-roles).

    x2_done/x3_done — отметки WP-406 (development.user_state.x2_completed_at/
    x3_completed_at), прокинуты в user_meta вместе с остальным batch-fetch
    get_nudge_candidates() (симметрично cp_profile_map для diagnost_bottleneck —
    без отдельного запроса per-user). Порог 3 дня — по образцу marathon_stalled/
    inactivity_3d (решение пилота 2026-07-20).
    """
    x2_done = user_meta.get('x2_done')
    x3_done = user_meta.get('x3_done')
    if x2_done and x3_done:
        return None  # Первокурсник достигнут — Онбордеру нечего напоминать

    account_created_at = user_meta.get('account_created_at')
    if not account_created_at:
        return None

    days_registered = (datetime.utcnow() - account_created_at).days
    if days_registered < 3:
        return None  # не застрял — ещё не прошло 3 дня с регистрации

    gap = "x2" if not x2_done else "x3"
    return {
        "nudge_type": f"nudge_onboarder_gap_{gap}",
        "gap": gap,
    }


@derived_rule("diagnost_bottleneck", cooldown_days=14)
def check_diagnost_bottleneck(engagement, user_meta, derived):
    """Узкое место из cp-профиля Диагноста — фокус-нудж на конкретный слот (WP-117 Ф-roles).

    derived['_cp_profile'] — transitional shim (не штатная ЦД-проекция): cp-профиль
    считается Диагностом и хранится в learning.cp_assessments, а не в 3_derived.
    Ключ с подчёркиванием — маркер, что scheduler кладёт его отдельно от digital_twins.
    """
    cp_profile = derived.get('_cp_profile') or {}
    bottleneck = cp_profile.get('bottleneck_slot')

    if not bottleneck or bottleneck == 'none':
        return None

    return {
        "nudge_type": f"nudge_bottleneck_{bottleneck.replace('.', '_')}",
        "bottleneck_slot": bottleneck,
        "recommended_stream": cp_profile.get('recommended_stream'),
    }


# ═══════════════════════════════════════════════════════════
# AI TEXT GENERATION (WP-117 Ф-roles)
# ═══════════════════════════════════════════════════════════

# Nudge types produced only by DERIVED_RULES/diagnost_bottleneck — eligible for
# AI-personalized text. Basic threshold/achievement rules keep static i18n text.
AI_PERSONALIZABLE_PREFIXES = (
    "nudge_stage_reached_",
    "nudge_agency_",
    "nudge_low_regularity",
    "nudge_reduce_frequency",
    "nudge_bottleneck_",
)


def is_ai_personalizable(nudge_key: str) -> bool:
    """Derived-aware nudge type eligible for Haiku-generated text (vs. static i18n)."""
    return nudge_key.startswith(AI_PERSONALIZABLE_PREFIXES)


_LANG_INSTRUCTION = {
    'ru': "ВАЖНО: Пиши ВСЁ на русском языке.",
    'en': "IMPORTANT: Write EVERYTHING in English.",
    'es': "IMPORTANTE: Escribe TODO en español.",
    'fr': "IMPORTANT: Écris TOUT en français.",
    'zh': "重要：请用中文书写所有内容。",
}


async def generate_derived_nudge_text(
    nudge_key: str,
    static_text: str,
    user_meta: dict,
    derived: dict,
    lang: str = 'ru',
) -> str:
    """Персонализировать текст derived-aware nudge через Haiku (WP-117 Ф-roles).

    Не решает, СЛАТЬ ли nudge (это работа check_fn/analyze) — только переписывает
    уже готовый static_text под конкретный ЦД-профиль пользователя. Fallback на
    static_text при любой ошибке/пустом ответе Claude — нудж не должен зависеть
    от доступности LLM (DP.RUNBOOK.001: graceful degradation).

    Args:
        nudge_key: конкретный ключ нуджа (например nudge_stage_reached_2)
        static_text: исходный i18n-текст — эталон смысла и длины для промпта
        user_meta: last_active_date, active_days_streak, marathon_status, ...
        derived: 3_derived JSONB (+ transitional _cp_profile)
        lang: язык ответа

    Returns:
        Персонализированный текст, либо static_text при сбое генерации.
    """
    from clients import claude
    from config import CLAUDE_MODEL_HAIKU

    qualification = derived.get('3_4_qualification') or {}
    integral = derived.get('3_10_integral') or {}
    agency = derived.get('3_1_agency') or {}

    profile_lines = [
        f"- Ступень: {qualification.get('stage', 'не определена')}",
        f"- Индекс агентности: {integral.get('index', 'нет данных')}",
        f"- Активных дней подряд: {user_meta.get('active_days_streak', 0)}",
        f"- Дней в неделю с занятиями: {agency.get('slot_days_per_week', 'нет данных')}",
    ]

    lang_instruction = _LANG_INSTRUCTION.get(lang, _LANG_INSTRUCTION['en'])

    system_prompt = f"""Ты — наставник, который пишет короткие подбадривающие уведомления.
{lang_instruction}

ЗАДАЧА: Перепиши сообщение ниже под профиль конкретного ученика — тот же смысл
и тон, но с опорой на его реальные данные. Не выдумывай факты, которых нет в профиле.
Сохрани длину исходного сообщения (1-2 предложения) и эмодзи в начале.

ИСХОДНОЕ СООБЩЕНИЕ:
{static_text}

ПРОФИЛЬ УЧЕНИКА:
{chr(10).join(profile_lines)}

Верни ТОЛЬКО текст сообщения, без пояснений и кавычек."""

    try:
        text = await claude.generate(
            system_prompt, "Персонализируй сообщение.",
            max_tokens=200, model=CLAUDE_MODEL_HAIKU,
        )
    except Exception as e:
        logger.warning(f"[Nudge] AI text generation failed for {nudge_key}: {e}")
        return static_text

    text = (text or "").strip()
    if not text:
        return static_text
    return text


# ═══════════════════════════════════════════════════════════
# ANALYZER
# ═══════════════════════════════════════════════════════════

def analyze(
    engagement: dict,
    user_meta: dict,
    derived: dict | None = None,
) -> list[dict]:
    """Проанализировать engagement данные, вернуть список nudge-рекомендаций.

    Args:
        engagement: данные из digital_twins.data['2_collected'] (5 групп)
        user_meta: данные из development.user_state (last_active_date, streak, etc.)
        derived: данные из digital_twins.data['3_derived'] (WP-151 Ф4)

    Returns:
        List of {rule_id, nudge_key, cooldown_days}
    """
    results = []

    # Basic rules (engagement + user_meta only)
    for rule_id, check_fn, cooldown_days in RULES:
        try:
            nudge_key = check_fn(engagement or {}, user_meta or {})
            if nudge_key:
                results.append({
                    'rule_id': rule_id,
                    'nudge_key': nudge_key,
                    'nudge_payload': {},
                    'cooldown_days': cooldown_days,
                })
        except Exception as e:
            logger.warning(f"[Nudge] Rule {rule_id} failed: {e}")

    # Derived-aware rules (engagement + user_meta + derived)
    if derived:
        for rule_id, check_fn, cooldown_days in DERIVED_RULES:
            try:
                result = check_fn(
                    engagement or {}, user_meta or {}, derived or {}
                )
                if not result:
                    continue
                # WP-349: правило может вернуть dict с nudge_type + payload.
                # Нормализуем для обратной совместимости: nudge_key всегда строка.
                if isinstance(result, dict):
                    nudge_key = result.get("nudge_type") or rule_id
                    nudge_payload = {k: v for k, v in result.items() if k != "nudge_type"}
                else:
                    nudge_key = result
                    nudge_payload = {}
                results.append({
                    'rule_id': rule_id,
                    'nudge_key': nudge_key,
                    'nudge_payload': nudge_payload,
                    'cooldown_days': cooldown_days,
                })
            except Exception as e:
                logger.warning(f"[Nudge] Derived rule {rule_id} failed: {e}")

    # WP-117 Ф-stopgap: achievement-нуджи отключены до починки источников данных.
    # Подавляем здесь, чтобы scheduler мог выбрать следующий допустимый кандидат,
    # а не тратил слот на "come back" для пользователя, у которого сегодня активность.
    filtered_results = []
    for nudge in results:
        reason = stopgap_suppression_reason(
            nudge["rule_id"], nudge["nudge_key"]
        )
        if reason:
            logger.info(
                "[WP-117 stopgap] Suppressed rule=%s nudge=%s reason=%s",
                nudge["rule_id"], nudge["nudge_key"], reason
            )
            continue
        filtered_results.append(nudge)

    return filtered_results
