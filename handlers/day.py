"""WP-428 Ф3: /day Day Open adapter for Telegram.

T3+ → pre-fetch rhythm from rewards DB + hermes_chat (active WPs, focus task).
T2  → CTA to connect IWE.
T<2 → CTA to subscribe.
Partial digest if hermes_chat fails: rhythm header + unavailable notice.
"""

import logging
from decimal import Decimal

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import Message

from db.queries import get_intern
from helpers.typing_indicator import keep_typing

logger = logging.getLogger(__name__)

day_router = Router(name="day")

_TIER_REQUIRED = 3
_SUBSCRIBE_MSG = "Открытие дня доступно с подпиской. Оформи: /subscribe"
_CONNECT_MSG = (
    "День открыт!\n\n"
    "Чтобы видеть активные задачи и рекомендацию — подключи IWE: /connect"
)
_UNAVAILABLE_MSG = "Сейчас недоступно. Попробуй позже."
_HERMES_PROMPT = (
    "Дай краткое открытие дня: 1-2 активных рабочих продукта (название + что дальше), "
    "одна конкретная фокус-задача на сегодня. Всего 3-5 строк."
)


def _fmt_pts(n: Decimal) -> str:
    """Format points with space as thousands separator."""
    return f"{int(n.to_integral_value()):,}".replace(",", " ")


@day_router.message(Command("day"))
async def on_day(message: Message) -> None:
    """Day Open digest: /day → rhythm (points) + active WPs + focus task."""
    chat_id = message.chat.id

    # Guard: skip while SM or feed digest awaits a reply
    from handlers.external_session import _sm_is_expecting_reply
    from states.feed.digest import FeedDigestState
    if await _sm_is_expecting_reply(chat_id) or FeedDigestState.is_waiting_fixation(chat_id):
        logger.info("[day] SM/feed expecting reply for chat %s — skipping", chat_id)
        raise SkipHandler

    intern = await get_intern(chat_id)
    if not intern or not intern.get("onboarding_completed"):
        await message.answer(_UNAVAILABLE_MSG)
        return

    from core.tier_detector import detect_ui_tier
    tier = await detect_ui_tier(chat_id)
    logger.info("[day] chat_id=%s tier=%s", chat_id, tier)

    if tier < 2:
        await message.answer(_SUBSCRIBE_MSG)
        return

    if tier < _TIER_REQUIRED:
        await message.answer(_CONNECT_MSG)
        return

    # T3+: pre-fetch rhythm from rewards DB (not in DT — rewards pool is separate)
    from helpers.dual_write import resolve_ory_id_from_chat
    from db.queries.rewards import get_earned_total, get_today_total

    ory_id = await resolve_ory_id_from_chat(chat_id)
    today_pts: Decimal = Decimal(0)
    earned_total: Decimal = Decimal(0)
    if ory_id:
        today_pts = await get_today_total(ory_id)
        earned_total = await get_earned_total(ory_id) or Decimal(0)
    else:
        logger.warning("[day] no ory_id for chat %s — rhythm shows zeros", chat_id)

    rhythm_header = (
        f"\U0001f4c5 Открытие дня\n\n"
        f"Ритм: {_fmt_pts(today_pts)} баллов сегодня  •  Всего: {_fmt_pts(earned_total)}\n\n"
    )

    from clients.gateway_mcp import gateway_mcp
    async with keep_typing(message):
        try:
            response = await gateway_mcp.hermes_chat(
                message=_HERMES_PROMPT,
                telegram_user_id=chat_id,
            )
        except Exception:
            logger.exception("[day] hermes_chat failed for chat %s", chat_id)
            response = None

    if not response:
        await message.answer(rhythm_header.rstrip() + "\n\n" + _UNAVAILABLE_MSG)
        return

    full_msg = rhythm_header + response
    try:
        await message.answer(full_msg, parse_mode="Markdown")
    except Exception:
        await message.answer(full_msg)
