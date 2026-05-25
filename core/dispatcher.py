"""
Центральный диспетчер — единая точка роутинга всех входящих сообщений.

Все entry points (команды, callbacks, scheduler) проходят через Dispatcher.
Добавление нового сервиса = добавить запись в core/services_init.py.
Добавление новой команды = добавить command в ServiceDescriptor.
"""

import logging
import time
from typing import Optional

from aiogram import Bot

from core.registry import registry

logger = logging.getLogger(__name__)

_sm_first_enter: set = set()  # chat_ids that completed first SM transition (process-scoped)


# Legacy COMMAND_MAP — fallback для команд, не зарегистрированных в реестре
_LEGACY_COMMAND_MAP = {
    'feed': 'feed.topics',
    'train': 'training.dashboard',
    'mode': 'common.mode_select',
    'language': 'common.settings',
    'assessment': 'workshop.assessment.flow',
}

# WP-156: Commands that route to consultation with a forced role
_ROLE_COMMAND_MAP = {
    'navigator': 'navigator',
}


class Dispatcher:
    """
    Центральный диспетчер бота.

    Единая точка роутинга, заменяющая разбросанные
    ``if state_machine is not None`` проверки в bot.py.
    """

    def __init__(self, state_machine, bot: Bot):
        self.sm = state_machine
        self.bot = bot

    @property
    def is_sm_active(self) -> bool:
        """State Machine инициализирована и доступна."""
        return self.sm is not None

    async def route_command(self, command: str, user: dict, context: dict = None) -> bool:
        """Роутинг команды → SM стейт.

        Сначала ищет в ServiceRegistry, затем fallback на legacy map.

        Args:
            context: Дополнительный контекст для стейта (например, source='mode').

        Returns:
            True если обработано через SM, False если нет маппинга.
        """
        from core.tracing import span

        if not self.sm:
            return False

        # 1. Ищем в реестре сервисов
        service = registry.resolve_command(command)
        if service:
            # Проверка доступа (подписка/триал)
            if service.access_check:
                chat_id = user.get('chat_id') if isinstance(user, dict) else getattr(user, 'chat_id', None)
                try:
                    has_access = await service.access_check(chat_id)
                    if not has_access:
                        from core.access import access_layer
                        lang = (user.get('language') or 'ru') if isinstance(user, dict) else 'ru'
                        text, kb = await access_layer.get_paywall(service.id, lang)
                        await self.bot.send_message(chat_id, text, reply_markup=kb)
                        return True
                except Exception as e:
                    logger.warning(f"[Dispatcher] Access check failed for {service.id}: {e}")

            target = service.get_entry_state(user)
            logger.info(f"[Dispatcher] route_command (registry): /{command} → {target}")
            await self.sm.go_to(user, target, context)
            return True

        # 2. WP-156: Role commands → consultation with force_role
        forced_role = _ROLE_COMMAND_MAP.get(command)
        if forced_role:
            logger.info(f"[Dispatcher] route_command (role): /{command} → common.consultation (force_role={forced_role})")
            role_context = dict(context or {})
            role_context['force_role'] = forced_role
            await self.sm.go_to(user, 'common.consultation', role_context)
            return True

        # 3. Fallback на legacy map
        target = _LEGACY_COMMAND_MAP.get(command)
        if target:
            logger.info(f"[Dispatcher] route_command (legacy): /{command} → {target}")
            await self.sm.go_to(user, target, context)
            return True

        return False

    async def route_learn(self, user: dict) -> None:
        """Единая точка входа для /learn — mode-aware.

        Использует ServiceRegistry для определения entry_state.
        """
        service = registry.get("marathon")
        if service:
            target = service.get_entry_state(user)
        else:
            # Fallback
            from core.helpers import get_user_mode_state
            target = get_user_mode_state(user)

        logger.info(f"[Dispatcher] route_learn: mode={user.get('mode')} → {target}")
        await self.sm.go_to(user, target)

    async def route_message(self, user: dict, message) -> bool:
        """Роутинг произвольных сообщений через SM.

        Returns:
            True если обработано.
        """
        if not self.sm:
            return False
        await self.sm.handle(user, message)
        return True

    async def route_callback(self, user: dict, callback) -> bool:
        """Роутинг callback query → SM.

        Returns:
            True если обработано.
        """
        if not self.sm:
            return False
        await self.sm.handle_callback(user, callback)
        return True

    async def route_scheduled(self, user: dict) -> None:
        """Роутинг для scheduler — mode-aware, аналогично route_learn."""
        await self.route_learn(user)

    async def go_to(self, user: dict, state_name: str, context: dict = None) -> None:
        """Прямой переход в указанный стейт SM."""
        if self.sm:
            chat_id = user.get('chat_id') if isinstance(user, dict) else None
            is_first = chat_id is not None and chat_id not in _sm_first_enter
            t0 = time.monotonic()
            await self.sm.go_to(user, state_name, context)
            elapsed_ms = (time.monotonic() - t0) * 1000
            if is_first:
                _sm_first_enter.add(chat_id)
                logger.info(f"[SM] cold-start go_to({state_name}) for {chat_id}: {elapsed_ms:.0f}ms (nav)")
            elif elapsed_ms > 2000:
                logger.warning(f"[SM] slow go_to({state_name}) for {chat_id}: {elapsed_ms:.0f}ms")
