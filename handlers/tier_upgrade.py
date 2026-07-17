"""
Хендлеры tier_upgrade — CTA-нуджи для переходов T1→T4 (WP-349 Ф4).

Содержит:
  nudge_post_diagnosis_s0      — B-low: ступень 1, предложить Марафон/S0
  nudge_post_diagnosis_sN      — B-high: ступень 2+, предложить S{N} поток
  nudge_subscription_cta       — C/E: активные дни или ступень ≥2, предложить подписку
  nudge_tier_suggest_t2        — D: ступень выросла, предложить T2
  nudge_personal_guide_cta     — F: 14+ дней + подписка → тайный гит (T2→T3)
  nudge_fullenv_cta            — G: 30+ дней + гид открыт → явный гит, VS Code (T3→T4)
  nudge_onboarding_gap_t2      — WP-117: T2 застрял (1-13 дней), не подключил AI-клиент

Callback «Позже» — ответить тостом + записать onboarding_snooze в domain_event.

Вызываются из core/scheduler.py (send_engagement_nudges) при наличии payload
suggest_tier_upgrade, либо напрямую как standalone-функции.

ЗАВИСИМОСТИ (Claude Ф1-Ф3, WP-349):
  - learning.onboarding_state содержит cp_stage, has_diagnosis, msg_b_low_sent_at, ...
  - engagement_analyzer.check_stage_upgrade() возвращает payload suggest_tier_upgrade
  - onboarding_controller.py (lock от Claude)
  - engagement_analyzer.py (lock от Claude)

НЕ ТРОГАЕТ:
  - onboarding_controller.py
  - engagement_analyzer.py
"""

import logging
import os

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import PLATFORM_URLS
from db.queries import get_intern
from db.queries.events import log_event
from i18n import t
from clients.gateway_mcp import gateway_mcp
from core.notification_service import enqueue, CLASS_CAPPED

logger = logging.getLogger(__name__)

tier_upgrade_router = Router(name="tier_upgrade")


# ═══════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ
# ═══════════════════════════════════════════════════════════

def _subscription_url() -> str:
    """URL страницы подписки из PLATFORM_URLS или env."""
    env_val = os.getenv("SUBSCRIPTION_WEB_URL", "").strip()
    if env_val:
        return env_val
    return PLATFORM_URLS.get("subscription", "https://system-school.ru/open-endedness")


def _lr_url() -> str:
    """URL программы личного развития (S0/Марафон)."""
    return PLATFORM_URLS.get("lr", "https://system-school.ru/programs/intro")


def _later_keyboard(snooze_key: str) -> InlineKeyboardMarkup:
    """Кнопка «Позже» для любого CTA-нуджа."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Позже",
            callback_data=f"tier_upgrade_snooze:{snooze_key}",
        )
    ]])


# ═══════════════════════════════════════════════════════════
# ПУБЛИЧНЫЕ ФУНКЦИИ-НУДЖИ (вызываются из scheduler)
# ═══════════════════════════════════════════════════════════

async def nudge_post_diagnosis_s0(bot, chat_id: int, lang: str = "ru") -> bool:
    """B-low: пилот ступень 1 — предложить начать с Марафона (S0).

    Returns True если сообщение отправлено, False при ошибке.
    """
    text = (
        "Вы на ступени 1. Начните с Марафона — 14 дней, каждый день 20 минут. "
        "Это самый быстрый способ встроить практику в расписание."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Начать Марафон",
                callback_data="tier_upgrade_start_marathon",
            ),
            InlineKeyboardButton(
                text="Позже",
                callback_data="tier_upgrade_snooze:b_low",
            ),
        ]
    ])
    try:
        await bot.send_message(chat_id, text, reply_markup=keyboard)
        logger.info(f"[TierUpgrade] nudge_post_diagnosis_s0 sent to {chat_id}")
        return True
    except Exception as e:
        logger.warning(f"[TierUpgrade] nudge_post_diagnosis_s0 failed for {chat_id}: {e}")
        return False


async def nudge_post_diagnosis_sN(bot, chat_id: int, cp_stage: int, lang: str = "ru") -> bool:
    """B-high: пилот ступень 2+ — предложить поток S{cp_stage}.

    Returns True если сообщение отправлено, False при ошибке.
    """
    n = max(1, cp_stage)  # S1 минимум
    text = (
        f"Ваша ступень {cp_stage}. Рекомендуем поток S{n} — "
        f"материалы и темп подобраны именно под ваш уровень."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"Попробовать S{n}",
                callback_data=f"tier_upgrade_start_stream:{n}",
            ),
            InlineKeyboardButton(
                text="Позже",
                callback_data="tier_upgrade_snooze:b_high",
            ),
        ]
    ])
    try:
        await bot.send_message(chat_id, text, reply_markup=keyboard)
        logger.info(f"[TierUpgrade] nudge_post_diagnosis_sN (stage={cp_stage}) sent to {chat_id}")
        return True
    except Exception as e:
        logger.warning(f"[TierUpgrade] nudge_post_diagnosis_sN failed for {chat_id}: {e}")
        return False


async def nudge_subscription_cta(
    bot,
    chat_id: int,
    activity_days: int,
    lang: str = "ru",
) -> bool:
    """C/E: пилот активен N дней или ступень ≥2 — предложить подписку.

    Returns True если сообщение отправлено, False при ошибке.
    """
    text = (
        f"Вы уже {activity_days} дней активны. "
        "Подписка открывает: личная база знаний, персональный план, больше инструментов."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Узнать о подписке",
                url=_subscription_url(),
            ),
            InlineKeyboardButton(
                text="Позже",
                callback_data="tier_upgrade_snooze:subscription_cta",
            ),
        ]
    ])
    try:
        await bot.send_message(chat_id, text, reply_markup=keyboard)
        logger.info(f"[TierUpgrade] nudge_subscription_cta (days={activity_days}) sent to {chat_id}")
        return True
    except Exception as e:
        logger.warning(f"[TierUpgrade] nudge_subscription_cta failed for {chat_id}: {e}")
        return False


async def nudge_tier_suggest_t2(
    bot,
    chat_id: int,
    cp_stage: int,
    lang: str = "ru",
) -> bool:
    """D: ступень выросла — предложить перейти на T2.

    Returns True если сообщение отправлено, False при ошибке.
    """
    # Список возможностей T2
    t2_features = [
        "расширенная лента материалов",
        "персональный план развития",
        "доступ к клубу",
    ]
    features_text = ", ".join(t2_features)

    text = (
        f"Ваша ступень выросла до {cp_stage}. "
        f"На T2 открывается: {features_text}."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Перейти на T2",
                url=_subscription_url(),
            ),
            InlineKeyboardButton(
                text="Позже",
                callback_data="tier_upgrade_snooze:t2",
            ),
        ]
    ])
    try:
        await bot.send_message(chat_id, text, reply_markup=keyboard)
        logger.info(f"[TierUpgrade] nudge_tier_suggest_t2 (stage={cp_stage}) sent to {chat_id}")
        return True
    except Exception as e:
        logger.warning(f"[TierUpgrade] nudge_tier_suggest_t2 failed for {chat_id}: {e}")
        return False


# ═══════════════════════════════════════════════════════════
# CALLBACK-ХЕНДЛЕРЫ
# ═══════════════════════════════════════════════════════════

@tier_upgrade_router.callback_query(F.data.startswith("tier_upgrade_snooze:"))
async def on_tier_upgrade_snooze(callback: CallbackQuery):
    """«Позже» на любом tier_upgrade нудже.

    1. Отвечает тостом.
    2. Записывает onboarding_snooze в domain_event (fire-and-forget).
    """
    snooze_key = callback.data.replace("tier_upgrade_snooze:", "")
    await callback.answer("Хорошо, напомним позже")

    # Убираем inline-кнопки у сообщения, но не удаляем само сообщение (§10.32)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    chat_id = callback.from_user.id
    # fire-and-forget: ошибка записи не ломает flow
    try:
        await log_event(
            chat_id,
            "onboarding_snooze",
            payload={"snooze_key": snooze_key},
            source="bot",
        )
    except Exception as e:
        logger.debug(f"[TierUpgrade] snooze log_event failed for {chat_id}: {e}")


@tier_upgrade_router.callback_query(F.data == "tier_upgrade_start_marathon")
async def on_tier_upgrade_start_marathon(callback: CallbackQuery):
    """«Начать Марафон» — роутим через dispatcher в learn/marathon flow."""
    await callback.answer()
    chat_id = callback.from_user.id

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    intern = await get_intern(chat_id)
    if not intern:
        return

    from handlers import get_dispatcher
    dispatcher = get_dispatcher()
    if dispatcher and dispatcher.is_sm_active:
        await dispatcher.route_command("learn", intern)
    else:
        lang = intern.get("language", "ru") or "ru"
        await callback.message.answer(t("errors.processing_error", lang))


@tier_upgrade_router.callback_query(F.data.startswith("tier_upgrade_start_stream:"))
async def on_tier_upgrade_start_stream(callback: CallbackQuery):
    """«Попробовать S{N}» — роутим через dispatcher (поток S{N})."""
    await callback.answer()
    stream_n = callback.data.replace("tier_upgrade_start_stream:", "")
    chat_id = callback.from_user.id

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    intern = await get_intern(chat_id)
    if not intern:
        return

    # Записываем намерение — dispatcher или navigator разберётся
    try:
        await log_event(
            chat_id,
            "tier_upgrade_stream_selected",
            payload={"stream": f"S{stream_n}"},
            source="bot",
        )
    except Exception:
        pass

    from handlers import get_dispatcher
    dispatcher = get_dispatcher()
    if dispatcher and dispatcher.is_sm_active:
        # Контекст stream передаётся через context-dict — dispatcher может использовать
        await dispatcher.route_command("learn", intern, context={"stream": f"S{stream_n}"})
    else:
        lang = intern.get("language", "ru") or "ru"
        await callback.message.answer(t("errors.processing_error", lang))


def _guide_web_url() -> str:
    """URL web-ридера для T3a (managed guide)."""
    return os.environ.get("GUIDE_WEB_URL", "https://guide.system-school.ru")


async def nudge_personal_guide_cta(
    bot,
    chat_id: int,
    activity_days: int,
    lang: str = "ru",
) -> bool:
    """F: подписка + 14+ дней активности, гид ещё не открыт → тайный гит (T2→T3).

    Returns True если сообщение отправлено, False при ошибке.
    """
    guide_url = _guide_web_url()
    content_spec = {
        "text": (
            f"Вы занимаетесь уже {activity_days} дней — самое время создать личную базу знаний.\n\n"
            "🤖 Платформа ведёт (проще — ничего не настраивать):\n"
            f"{guide_url}/start\n\n"
            "🔧 Вы ведёте сами (полный контроль): /personal-guide-start\n\n"
            "Можно начать с платформой и перейти на ручное управление позже."
        ),
        "actions": [
            {"label": "Начать с платформой", "url": f"{guide_url}/start"},
            {"label": "Позже", "action": "tier_upgrade_snooze:personal_guide"},
        ],
    }
    try:
        result = await enqueue(
            chat_id, CLASS_CAPPED, content_spec,
            dedup_key=f"nudge_f:{chat_id}",
            journal_key=f"nudge_f:{chat_id}",
            journal_type="nudge",
        )
        accepted = result.get("status") == "queued"
        if accepted:
            logger.info("[TierUpgrade] nudge_personal_guide_cta (days=%d) enqueued for %d", activity_days, chat_id)
            # WP-349 Ф30: gateway intent only when message is actually queued (H1 fix)
            try:
                gw = await gateway_mcp.request_equipment_upgrade(
                    telegram_user_id=chat_id,
                    target_tier="T3",
                    channel="telegram",
                    trigger="nudge_f",
                )
                if gw and gw.get("success"):
                    logger.info("[TierUpgrade] T3 request recorded for %s", chat_id)
            except Exception as exc:
                logger.warning("[TierUpgrade] gateway request_equipment_upgrade failed: %s", exc)
        else:
            logger.info("[TierUpgrade] nudge_personal_guide_cta suppressed for %d: %s", chat_id, result.get("reason"))
        return accepted
    except Exception as e:
        logger.warning("[TierUpgrade] nudge_personal_guide_cta failed for %d: %s", chat_id, e)
        return False


async def nudge_fullenv_cta(
    bot,
    chat_id: int,
    activity_days: int,
    lang: str = "ru",
) -> bool:
    """G: подписка + гид открыт + 30+ дней активности → явный гит (T3→T4, VS Code + полная среда).

    Returns True если сообщение отправлено, False при ошибке.
    """
    content_spec = {
        "text": (
            f"Вы занимаетесь уже {activity_days} дней и ваша база знаний растёт. "
            "Следующий уровень — полное окружение IWE.\n\n"
            "Подключите VS Code для глубокой работы: выполните /connect и следуйте инструкции."
        ),
        "actions": [
            {"label": "Подключить VS Code", "action": "tier_upgrade_fullenv_start"},
            {"label": "Позже", "action": "tier_upgrade_snooze:fullenv"},
        ],
    }
    try:
        result = await enqueue(
            chat_id, CLASS_CAPPED, content_spec,
            dedup_key=f"nudge_g:{chat_id}",
            journal_key=f"nudge_g:{chat_id}",
            journal_type="nudge",
        )
        accepted = result.get("status") == "queued"
        if accepted:
            logger.info("[TierUpgrade] nudge_fullenv_cta (days=%d) enqueued for %d", activity_days, chat_id)
            # WP-349 Ф30: gateway intent only when message is actually queued (H1 fix)
            try:
                gw = await gateway_mcp.request_equipment_upgrade(
                    telegram_user_id=chat_id,
                    target_tier="T4",
                    channel="telegram",
                    trigger="nudge_g",
                )
                if gw and gw.get("success"):
                    logger.info("[TierUpgrade] T4 request recorded for %s", chat_id)
            except Exception as exc:
                logger.warning("[TierUpgrade] gateway request_equipment_upgrade failed: %s", exc)
        else:
            logger.info("[TierUpgrade] nudge_fullenv_cta suppressed for %d: %s", chat_id, result.get("reason"))
        return accepted
    except Exception as e:
        logger.warning("[TierUpgrade] nudge_fullenv_cta failed for %d: %s", chat_id, e)
        return False


async def nudge_onboarding_gap_t2(
    bot,
    chat_id: int,
    activity_days: int,
    lang: str = "ru",
) -> bool:
    """WP-117: T2 subscribed but stuck before F threshold (1-13 active days, no AI client).

    Fires when user paid but hasn't connected claude.ai / VS Code yet.
    Uses 48h CLASS_CAPPED cooldown via dedup_key.
    Returns True if message was enqueued, False otherwise.
    """
    content_spec = {
        "text": (
            "Ты на уровне «Изучение» — но персональное руководство ещё не работает.\n\n"
            "Оно включается за 2 минуты: открой claude.ai, подключи Aisystant → /connect"
        ),
        "actions": [
            {"label": "Как подключить", "action": "tier_upgrade_show_connect_guide"},
            {"label": "Позже", "action": "tier_upgrade_snooze:onboarding_gap"},
        ],
    }
    try:
        result = await enqueue(
            chat_id, CLASS_CAPPED, content_spec,
            dedup_key=f"nudge_onboarding_gap:{chat_id}",
            journal_key=f"nudge_onboarding_gap:{chat_id}",
            journal_type="nudge",
        )
        accepted = result.get("status") == "queued"
        if accepted:
            logger.info(
                "[TierUpgrade] nudge_onboarding_gap_t2 (days=%d) enqueued for %d",
                activity_days, chat_id,
            )
        else:
            logger.info(
                "[TierUpgrade] nudge_onboarding_gap_t2 suppressed for %d: %s",
                chat_id, result.get("reason"),
            )
        return accepted
    except Exception as e:
        logger.warning("[TierUpgrade] nudge_onboarding_gap_t2 failed for %d: %s", chat_id, e)
        return False


# Mapping upgrade marker → specialised sender with rich CTA.
# Used in send_engagement_nudges() (scheduler) instead of generic send_tg_nudge().
# Contract: sender(bot, chat_id, activity_days, lang="ru") -> bool
UPGRADE_NUDGE_SENDERS: dict = {
    "f": nudge_personal_guide_cta,
    "g": nudge_fullenv_cta,
    "onboarding_gap": nudge_onboarding_gap_t2,
}


@tier_upgrade_router.callback_query(F.data == "tier_upgrade_fullenv_start")
async def on_tier_upgrade_fullenv_start(callback: CallbackQuery):
    """«Подключить VS Code» → запрос T4 через gateway + направляем к /connect."""
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # WP-349 Ф30: записать intent апгрейда через gateway MCP
    chat_id = callback.from_user.id
    try:
        result = await gateway_mcp.request_equipment_upgrade(
            telegram_user_id=chat_id,
            target_tier="T4",
            channel="telegram",
            trigger="user_action",
        )
        if result and result.get("success"):
            logger.info("[TierUpgrade] T4 request recorded for %s", chat_id)
        else:
            logger.warning("[TierUpgrade] T4 request failed for %s: %s", chat_id, result)
    except Exception as exc:
        logger.warning("[TierUpgrade] gateway request_equipment_upgrade failed: %s", exc)

    await callback.message.answer(
        "Выполните /connect в этом чате — бот проверит подключение и обновит ваш путь."
    )
