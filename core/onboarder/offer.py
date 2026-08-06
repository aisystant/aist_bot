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
_OFFER_KEY = "offer_shown_at"
_CALLBACK_DATA = "onboarder_start"

_OFFER_TEXT = (
    "Хочешь освоиться в сообществе? За пару минут покажу, как тут всё "
    "устроено и как пользоваться ботом, и помогу выбрать первый курс."
)
_BUTTON_TEXT = "🎓 Освоиться"


def _on_cooldown(offered_at_iso) -> bool:
    """Был ли оффер показан недавно (внутри окна cooldown). Битый timestamp → не на cooldown."""
    if not offered_at_iso:
        return False
    try:
        dt = datetime.datetime.fromisoformat(offered_at_iso)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    age_days = (datetime.datetime.utcnow() - dt).total_seconds() / 86400
    return age_days < _COOLDOWN_DAYS


async def should_offer(chat_id: int) -> bool:
    """Показывать ли оффер «Освоиться»: открыт разрыв Х2/Х3 И не на cooldown.

    Чистый read-path: только читает (статус + контекст), ничего не пишет.
    """
    from core.onboarder import storage

    status = await storage.get_status(chat_id)
    has_gap = not (status["x2_done"] and status["x3_done"])
    if not has_gap:
        return False
    ctx = await storage.get_onboarding_context(chat_id)
    return not _on_cooldown(ctx.get(_OFFER_KEY))


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
