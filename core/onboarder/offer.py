"""
Оффер «Освоиться» — гейт показа кнопки входа Онбордера (WP-406 Ф5).

# see DP.SC.170, DP.ROLE.067

Слоистость (консенсус peer-сессии 2026-06-11-20): core решает «что и когда»,
handlers — «как нарисовать». Поэтому здесь — чистые решения без aiogram:
  - should_offer(chat_id): показывать ли оффер (есть открытый разрыв Х2/Х3 И
    не на cooldown). Read-path без side effects.
  - mark_offered(chat_id): записать факт показа (side-effect отдельно от проверки).
  - offer_payload(): plain-dict с текстом и кнопкой — handlers оборачивают в
    InlineKeyboardMarkup.

Cooldown не даёт спамить флот: у всех существующих пользователей разрыв Х2/Х3
открыт (колонки x2/x3_completed_at = NULL), поэтому без cooldown оффер всплывал
бы на каждом /start. До проактивных нуджей scheduler'а (Ф6) это единственный
re-offer канал.
"""

import datetime
import logging

logger = logging.getLogger(__name__)

_COOLDOWN_DAYS = 3
_DEDUP_SECONDS = 120
_OFFER_KEY = "offer_shown_at"
_CALLBACK_DATA = "onboarder_start"

_OFFER_TEXT = (
    "Хочешь освоиться в сообществе? За пару минут покажу, как тут всё "
    "устроено и как пользоваться ботом, и помогу выбрать первый курс."
)
_BUTTON_TEXT = "🎓 Освоиться"


def _seconds_since(offered_at_iso) -> float | None:
    """Секунд с момента offered_at_iso. Битый/пустой timestamp → None."""
    if not offered_at_iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(offered_at_iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return (datetime.datetime.utcnow() - dt).total_seconds()


def _on_cooldown(offered_at_iso) -> bool:
    """Был ли оффер показан недавно (внутри окна cooldown). Битый timestamp → не на cooldown."""
    age_s = _seconds_since(offered_at_iso)
    if age_s is None:
        return False
    return age_s / 86400 < _COOLDOWN_DAYS


def _just_shown(offered_at_iso) -> bool:
    """Оффер показан секунды назад (другим каналом) — дедуп-окно, отдельное от cooldown.

    Применяется ДАЖЕ при bypass_cooldown=True: защищает от дубля, если Проводник
    (handlers/hermes.py, свой cooldown) и /start (bypass_cooldown) показывают
    оффер почти одновременно — найдено code review peer-сессии 2026-08-12-12.
    """
    age_s = _seconds_since(offered_at_iso)
    if age_s is None:
        return False
    return age_s < _DEDUP_SECONDS


async def should_offer(chat_id: int, bypass_cooldown: bool = False) -> bool:
    """Показывать ли оффер «Освоиться»: открыт разрыв Х2/Х3 И (не на cooldown ИЛИ bypass_cooldown).

    Чистый read-path: только читает (статус + контекст), ничего не пишет.

    `bypass_cooldown`: для вызовов из `/start` (peer-сессия 2026-08-12-12,
    находка WP-406 — живой прогон 12.08). Cooldown защищает от повторного
    показа офера на КАЖДЫЙ онбординг-relevant вопрос Проводнику (handlers/hermes.py),
    но на `/start` он же гасит единственный явный next-step CTA на 3 дня, если
    пользователь уже видел оффер и не нажал (естественное поведение — зашёл
    повторно, изучая бота). `/start` — явное действие пользователя, не фоновый
    нудж, поэтому для него cooldown не применяется.
    """
    from core.onboarder import storage

    status = await storage.get_status(chat_id)
    has_gap = not (status["x2_done"] and status["x3_done"])
    if not has_gap:
        logger.debug("[onboarder] should_offer chat=%s has_gap=False -> False", chat_id)
        return False
    ctx = await storage.get_onboarding_context(chat_id)
    offered_at = ctx.get(_OFFER_KEY)
    if _just_shown(offered_at):
        logger.debug(
            "[onboarder] should_offer chat=%s has_gap=True offered_at=%s just_shown=True -> False",
            chat_id, offered_at,
        )
        return False
    cooldown_hit = _on_cooldown(offered_at)
    result = bypass_cooldown or not cooldown_hit
    logger.debug(
        "[onboarder] should_offer chat=%s has_gap=True offered_at=%s cooldown_hit=%s "
        "bypass_cooldown=%s -> %s",
        chat_id, offered_at, cooldown_hit, bypass_cooldown, result,
    )
    return result


async def mark_offered(chat_id: int) -> None:
    """Записать факт показа оффера (для cooldown). Вызывать после фактической отправки."""
    from core.onboarder import storage

    await storage.save_onboarding_context(
        chat_id, {_OFFER_KEY: datetime.datetime.utcnow().isoformat()}
    )


def offer_payload() -> dict:
    """Plain-data оффера — handlers оборачивают button_text/callback_data в клавиатуру."""
    return {
        "text": _OFFER_TEXT,
        "button_text": _BUTTON_TEXT,
        "callback_data": _CALLBACK_DATA,
    }
