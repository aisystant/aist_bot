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
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class AccessLayer:
    """Слой доступа для проверки прав пользователя на сервисы."""

    async def has_access(self, user_id: int, service_id: str) -> bool:
        """Проверяет, имеет ли пользователь доступ к сервису.

        На текущей фазе: все сервисы доступны всем (free tier).
        В будущем: проверка подписки, покупок, feature flags.
        """
        # TODO: Фаза 4.2 — проверка в таблице user_access
        # Сейчас: все сервисы доступны (open beta)
        return True

    async def get_paywall(self, service_id: str, lang: str = "ru") -> Optional[str]:
        """Получить текст paywall для недоступного сервиса.

        Returns:
            Текст paywall или None если сервис бесплатный.
        """
        # TODO: Фаза 4.2 — текст paywall из конфига
        return None

    async def check_subscription(self, user_id: int) -> Optional[dict]:
        """Проверить подписку пользователя.

        Returns:
            Словарь с информацией о подписке или None.
        """
        # TODO: Фаза 4.2 — запрос в БД
        return None

    async def check_purchase(self, user_id: int, resource_id: str) -> bool:
        """Проверить, куплен ли ресурс (курс, фича).

        Returns:
            True если куплено.
        """
        # TODO: Фаза 4.2 — запрос в БД
        return False

    async def grant_access(
        self,
        user_id: int,
        access_type: str,
        resource_id: str,
        expires_at: Optional[datetime] = None,
    ) -> None:
        """Выдать доступ пользователю.

        Args:
            user_id: ID пользователя
            access_type: "subscription", "purchase", "feature"
            resource_id: ID ресурса (service_id, course_id, feature_id)
            expires_at: Дата истечения (None = бессрочно)
        """
        # TODO: Фаза 4.2 — INSERT в user_access
        logger.info(f"[Access] grant: user={user_id}, type={access_type}, resource={resource_id}")

    async def revoke_access(self, user_id: int, resource_id: str) -> None:
        """Отозвать доступ."""
        # TODO: Фаза 4.2 — DELETE/UPDATE в user_access
        logger.info(f"[Access] revoke: user={user_id}, resource={resource_id}")


# Глобальный экземпляр
access_layer = AccessLayer()
