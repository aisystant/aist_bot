"""
Распознавание онбординг-интентов из свободного текста (WP-349 Ф22).

Активируется из fallback.py когда пользователь онбордирован и SM-стейт не активен.
Маршрутизирует в те же handlers что и inline-кнопки intent:* из consent.py.
"""

from __future__ import annotations

import logging
import re
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

from config.onboarding_intents import ONBOARDING_INTENT_MAP, NEGATIVE_PATTERNS

logger = logging.getLogger(__name__)


def detect_onboarding_intent(text: str | None) -> str | None:
    """Определяет онбординг-интент из свободного текста.

    Сначала проверяет negative-patterns («не хочу марафон» → None).
    Затем ищет keyword по word-boundary (re.UNICODE) для снижения false-positives.
    """
    if text is None:
        return None
    text_lower = text.lower().strip()

    for neg_pattern in NEGATIVE_PATTERNS:
        if re.search(neg_pattern, text_lower):
            return None

    for intent, keywords in ONBOARDING_INTENT_MAP.items():
        for kw in keywords:
            pattern = rf'(?<!\w){re.escape(kw)}(?!\w)'
            if re.search(pattern, text_lower):
                return intent
    return None


async def route_onboarding_intent(
    text: str,
    chat_id: int,
    message: Message,
    state: FSMContext,
) -> bool:
    """Маршрутизирует онбординг-интент в нужный handler.

    Возвращает True если интент распознан и обработан, False при ошибке или no-match.
    Вызывает те же функции что и inline-кнопки intent:* (consent.py).
    """
    intent = detect_onboarding_intent(text)
    if not intent:
        return False

    logger.info("[Ф22] chat_id=%s text=%r → intent=%s", chat_id, text[:40], intent)

    try:
        if intent == "marathon":
            from handlers.marathon import start_marathon_flow
            await start_marathon_flow(chat_id, message)

        elif intent == "diagnose":
            from handlers.diagnose import cmd_diagnose
            await cmd_diagnose(message, state)

        elif intent == "setup":
            from handlers.setup import send_setup_screen
            await send_setup_screen(chat_id, message)

    except Exception:
        logger.exception("[Ф22] handler failed intent=%s chat_id=%s", intent, chat_id)
        return False

    return True
