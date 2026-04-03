"""
Слой доступа — контролирует видимость сервисов для пользователя.

Биллинг = слой доступа, не домен в меню (DP.AISYS.014 § 4.4).

Модель доступа (WP-85, 2026-03-12):
  1. Бесплатный сервис (не в LOCKED_SERVICES) → True
  2. Подписка «Бесконечное развитие» на Aisystant (system-school.ru) → True
  3. В пределах 30-дневного триала от /start → True (T2 features)
  4. Иначе → False (paywall)

TG Stars = донаты (благодарность), НЕ влияют на доступ.
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

        Логика (WP-85):
        1. Бесплатный сервис → True
        2. Активная подписка БР на Aisystant → True
        3. В пределах триала (30 дней от /start) → True
        4. Иначе → False

        TG Stars донаты НЕ влияют на доступ.
        """
        if service_id not in LOCKED_SERVICES:
            return True

        # Aisystant «Бесконечное развитие»
        if await self._has_aisystant_subscription(user_id):
            return True

        return await self.is_in_trial(user_id)

    async def _has_aisystant_subscription(self, user_id: int) -> bool:
        """Проверить подписку БР через Aisystant API."""
        try:
            from db.queries.aisystant import get_aisystant_id
            aisystant_id = await get_aisystant_id(user_id)
            if not aisystant_id:
                return False
            from clients.aisystant import aisystant
            return await aisystant.has_active_subscription(aisystant_id)
        except Exception as e:
            logger.warning(f"[Access] Aisystant subscription check failed: {e}")
            return False

    async def is_in_trial(self, user_id: int) -> bool:
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

        WP-79: Paywall ведёт на Aisystant «Бесконечное развитие».
        Telegram Stars = донаты (не подписка).
        П2: Контекстный paywall — описание конкретного сервиса.

        Returns:
            (text, keyboard) — сообщение и кнопка подписки.
        """
        # Контекстное описание сервиса (если есть)
        context_key = f'paywall.{service_id}'
        context_text = t(context_key, lang)
        if context_text.startswith('paywall.'):
            # Ключ не найден — fallback на generic
            context_text = ""

        if context_text:
            text = context_text + "\n\n" + t('paywall.trial_hint', lang)
        else:
            text = t('aisystant_sub.title', lang) + "\n\n" + t('aisystant_sub.desc', lang)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=t('aisystant_sub.btn_subscribe', lang),
                callback_data="aisystant_subscribe",
            )]
        ])
        return text, keyboard

    async def get_paywall_with_register(self, user_id: int, service_id: str, lang: str = "ru") -> tuple[str, InlineKeyboardMarkup]:
        """Paywall с кнопкой регистрации для T0 (WP-187).

        Если пользователь T0 (нет ory_id) — показывает кнопку регистрации.
        Если T1+ — стандартный paywall с подпиской.
        """
        text, _ = await self.get_paywall(service_id, lang)

        buttons = []

        # Check if user needs Ory registration (T0)
        try:
            from db.queries.identity import get_user_by_telegram
            user = await get_user_by_telegram(user_id)
            if user and not user.get("ory_id"):
                buttons.append([InlineKeyboardButton(
                    text="Зарегистрироваться на платформе",
                    callback_data="ory_register",
                )])
        except Exception:
            pass

        buttons.append([InlineKeyboardButton(
            text=t('aisystant_sub.btn_subscribe', lang),
            callback_data="aisystant_subscribe",
        )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        return text, keyboard

    async def check_subscription(self, user_id: int) -> Optional[dict]:
        """Проверить подписку пользователя.

        Returns:
            Словарь с информацией о подписке или None.
        """
        from db.queries.subscription import get_active_subscription
        return await get_active_subscription(user_id)

    async def check_purchase(self, user_id: int, resource_id: str) -> bool:
        """Проверить, куплен ли ресурс (программа, фича).

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
