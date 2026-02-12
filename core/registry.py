"""
Сервисный реестр — центральная точка управления сервисами бота.

menu(user) = registry.filter(access).render()

Добавление нового сервиса:
1. Создать ServiceDescriptor в core/services_init.py
2. Меню обновится автоматически — ноль изменений в UI-коде
"""

import logging
from typing import Optional

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from core.services import ServiceDescriptor
from core import callback_protocol
from i18n import t

logger = logging.getLogger(__name__)


class ServiceRegistry:
    """Реестр сервисов бота.

    Хранит все зарегистрированные сервисы и предоставляет:
    - Фильтрацию по доступу пользователя
    - Генерацию inline keyboard из доступных сервисов
    - Резолвинг команд и callback'ов в сервисы
    """

    def __init__(self):
        self._services: dict[str, ServiceDescriptor] = {}
        self._command_index: dict[str, ServiceDescriptor] = {}

    def register(self, service: ServiceDescriptor) -> None:
        """Зарегистрировать сервис."""
        self._services[service.id] = service
        # Индексируем основную команду
        if service.command:
            cmd = service.command.lstrip('/')
            self._command_index[cmd] = service
        # Индексируем дополнительные команды
        for cmd in service.commands:
            self._command_index[cmd.lstrip('/')] = service
        logger.debug(f"Registered service: {service.id}")

    def get(self, service_id: str) -> Optional[ServiceDescriptor]:
        """Получить сервис по id."""
        return self._services.get(service_id)

    def get_all(self) -> list[ServiceDescriptor]:
        """Все зарегистрированные сервисы."""
        return list(self._services.values())

    async def for_user(self, user: dict, category: str = None) -> list[ServiceDescriptor]:
        """Сервисы, доступные пользователю.

        Фильтрует по:
        - category (если указана)
        - access_check (если задана у сервиса)
        - feature_flag (будущий биллинг)

        Сортирует по order.
        """
        result = []
        user_id = user.get('chat_id') if isinstance(user, dict) else getattr(user, 'chat_id', None)

        for service in self._services.values():
            # Фильтр по visible
            if not service.visible:
                continue

            # Фильтр по категории
            if category and service.category != category:
                continue

            # Фильтр по access_check
            if service.access_check:
                try:
                    has_access = await service.access_check(user_id)
                    if not has_access:
                        continue
                except Exception as e:
                    logger.warning(f"Access check failed for {service.id}: {e}")
                    continue

            result.append(service)

        return sorted(result, key=lambda s: s.order)

    async def build_menu(
        self,
        user: dict,
        category: str = None,
        columns: int = 1,
    ) -> InlineKeyboardMarkup:
        """Генерирует inline keyboard из доступных сервисов.

        Args:
            user: Пользователь
            category: Фильтр по категории
            columns: Количество кнопок в строке (1 или 2)
        """
        lang = user.get('language', 'ru') if isinstance(user, dict) else getattr(user, 'language', 'ru') or 'ru'

        services = await self.for_user(user, category)

        buttons = []
        row = []
        for service in services:
            label = f"{service.icon} {t(service.i18n_key, lang)}"
            btn = InlineKeyboardButton(
                text=label,
                callback_data=callback_protocol.encode("service", service.id),
            )
            row.append(btn)
            if len(row) >= columns:
                buttons.append(row)
                row = []

        if row:
            buttons.append(row)

        return InlineKeyboardMarkup(inline_keyboard=buttons)

    def resolve_command(self, command: str) -> Optional[ServiceDescriptor]:
        """Найти сервис по slash-команде.

        Args:
            command: Команда без "/" (например "learn", "rp")
        """
        return self._command_index.get(command)

    def resolve_callback(self, callback_data: str) -> Optional[ServiceDescriptor]:
        """Найти сервис по callback_data формата 'service:{id}'.

        Args:
            callback_data: Строка callback_data от Telegram
        """
        if callback_protocol.matches(callback_data, "service"):
            _, service_id, _ = callback_protocol.decode(callback_data)
            return self._services.get(service_id)
        return None

    async def record_usage(self, user_id: int, service_id: str, action: str = "enter") -> None:
        """Записать использование сервиса для аналитики.

        Args:
            user_id: ID пользователя
            service_id: ID сервиса
            action: Тип действия ("enter", "complete", ...)
        """
        try:
            from db.queries.activity import record_service_usage
            await record_service_usage(user_id, service_id, action)
        except Exception as e:
            # Аналитика не должна ломать основной флоу
            logger.debug(f"Failed to record usage: {e}")


# Глобальный экземпляр реестра
registry = ServiceRegistry()
