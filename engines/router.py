"""
EngineRouter — единственная точка выбора адаптера Портного (WP-262 В1.1).

Читает TAILOR_ROUTE env var. Остальной код не знает о routing.

TAILOR_ROUTE=local (default) → BotTailorAdapter (локальная сборка)
TAILOR_ROUTE=platform         → PlatformTailorAdapter (Railway FastAPI)
"""

import logging
import os
from typing import Union

from aiogram import Bot

from engines.tailor.bot_adapter import BotTailorAdapter
from engines.tailor.platform_adapter import PlatformTailorAdapter

logger = logging.getLogger(__name__)

_ENV_ROUTE = "TAILOR_ROUTE"
_ENV_URL = "TAILOR_SERVICE_URL"
_ENV_SECRET = "TAILOR_SHARED_SECRET"


class EngineRouter:
    """Маршрутизатор адаптеров движков (Ф1 strangler, WP-262)."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    def get_tailor_adapter(
        self, user_id: int
    ) -> Union[BotTailorAdapter, PlatformTailorAdapter]:
        """Вернуть адаптер Портного для user_id.

        При TAILOR_ROUTE=platform и отсутствующем TAILOR_SERVICE_URL
        делает fallback на local с предупреждением.
        """
        route = os.getenv(_ENV_ROUTE, "local")

        if route == "platform":
            url = os.getenv(_ENV_URL, "")
            if not url:
                logger.error(
                    "[EngineRouter] TAILOR_SERVICE_URL not set, falling back to local"
                )
                return BotTailorAdapter(bot=self._bot)
            secret = os.getenv(_ENV_SECRET, "")
            logger.debug("[EngineRouter] user=%d → platform %s", user_id, url)
            return PlatformTailorAdapter(url=url, shared_secret=secret)

        logger.debug("[EngineRouter] user=%d → local", user_id)
        return BotTailorAdapter(bot=self._bot)
