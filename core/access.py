"""
Слой доступа — контролирует видимость сервисов для пользователя.

Биллинг = слой доступа, не домен в меню (DP.AISYS.014 § 4.4).

3 типа доступа:
  - subscription: рекуррентная подписка → набор доступных сервисов
  - purchase: разовая покупка → открывает конкретный курс
  - feature: paywall на функцию → показывает paywall при доступе

user.has_access(service) = user.subscription ∪ user.purchases ∪ user.role
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config.settings import LOCKED_SERVICES, FREE_TRIAL_DAYS
from i18n import t

logger = logging.getLogger(__name__)


class AccessLayer:
    """Слой доступа для проверки прав пользователя на сервисы."""

    async def has_access(self, user_id: int, service_id: str) -> bool:
        """Проверяет, имеет ли пользователь доступ к сервису.

        Логика:
        0. До даты запуска подписки → True (open beta)
        1. Бесплатный сервис → True
        2. Активная подписка → True
        3. В пределах триала → True
        4. Иначе → False
        """
        # До запуска подписки — всё бесплатно
        from config.settings import SUBSCRIPTION_LAUNCH_DATE
        from datetime import date
        if date.today() < SUBSCRIPTION_LAUNCH_DATE:
            return True

        if service_id not in LOCKED_SERVICES:
            return True

        from db.queries.subscription import is_subscribed
        if await is_subscribed(user_id):
            return True

        return await self._is_in_trial(user_id)

    async def _is_in_trial(self, user_id: int) -> bool:
        """Проверить, находится ли пользователь в пределах бесплатного триала."""
        from db.queries import get_intern
        user = await get_intern(user_id)

        trial_start = user.get('trial_started_at') or user.get('created_at')
        if trial_start is None:
            return True  # новый пользователь, ещё не завершил онбординг

        # Приводим к aware datetime
        if hasattr(trial_start, 'tzinfo') and trial_start.tzinfo is None:
            trial_start = trial_start.replace(tzinfo=timezone.utc)

        trial_end = trial_start + timedelta(days=FREE_TRIAL_DAYS)
        return datetime.now(timezone.utc) < trial_end

    async def get_trial_days_remaining(self, user_id: int) -> int:
        """Сколько дней осталось в триале. 0 = триал истёк."""
        from db.queries import get_intern
        user = await get_intern(user_id)

        trial_start = user.get('trial_started_at') or user.get('created_at')
        if trial_start is None:
            return FREE_TRIAL_DAYS

        if hasattr(trial_start, 'tzinfo') and trial_start.tzinfo is None:
            trial_start = trial_start.replace(tzinfo=timezone.utc)

        trial_end = trial_start + timedelta(days=FREE_TRIAL_DAYS)
        remaining = (trial_end - datetime.now(timezone.utc)).days
        return max(0, remaining)

    async def get_paywall(self, service_id: str, lang: str = "ru") -> tuple[str, InlineKeyboardMarkup]:
        """Получить текст paywall и кнопку подписки.

        Returns:
            (text, keyboard) — сообщение и кнопка подписки.
        """
        from core.pricing import get_current_price

        price = get_current_price()
        text = t('subscription.paywall_text', lang, price=price)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=t('subscription.subscribe_button', lang, price=price),
                callback_data="subscribe",
            )]
        ])
        return text, keyboard

    async def check_subscription(self, user_id: int) -> Optional[dict]:
        """Проверить подписку пользователя.

        Returns:
            Словарь с информацией о подписке или None.
        """
        from db.queries.subscription import get_active_subscription
        return await get_active_subscription(user_id)

    async def check_purchase(self, user_id: int, resource_id: str) -> bool:
        """Проверить, куплен ли ресурс (курс, фича).

        Returns:
            True если куплено.
        """
        # TODO: Phase 4.3 — purchases
        return False

    async def grant_access(
        self,
        user_id: int,
        access_type: str,
        resource_id: str,
        expires_at: Optional[datetime] = None,
    ) -> None:
        """Выдать доступ пользователю."""
        logger.info(f"[Access] grant: user={user_id}, type={access_type}, resource={resource_id}")

    async def revoke_access(self, user_id: int, resource_id: str) -> None:
        """Отозвать доступ."""
        logger.info(f"[Access] revoke: user={user_id}, resource={resource_id}")


# Глобальный экземпляр
access_layer = AccessLayer()
