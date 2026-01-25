"""
State Machine — центральный диспетчер состояний бота.

Загружает переходы из config/transitions.yaml и управляет
переходами между стейтами.

Использование:
    from core.machine import StateMachine

    machine = StateMachine()
    machine.load_transitions("config/transitions.yaml")
    machine.register_all(states)

    # Обработка сообщения
    await machine.handle(user, message)
"""

import logging
from pathlib import Path
from typing import Optional

import yaml

from states.base import BaseState

logger = logging.getLogger(__name__)


class StateMachine:
    """
    Центральный диспетчер состояний.

    Отвечает за:
    - Регистрацию стейтов
    - Загрузку переходов из YAML
    - Определение текущего стейта пользователя
    - Обработку событий и переходы
    """

    def __init__(self):
        self._states: dict[str, BaseState] = {}
        self._transitions: dict[str, dict] = {}
        self._global_events: dict[str, dict] = {}
        self._default_state: str = "common.start"

    def load_transitions(self, path: str | Path) -> None:
        """
        Загружает таблицу переходов из YAML.

        Args:
            path: Путь к файлу transitions.yaml
        """
        path = Path(path)
        if not path.exists():
            logger.warning(f"Файл переходов не найден: {path}")
            return

        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        self._transitions = data.get('states', {})
        self._global_events = data.get('global_events', {})

        logger.info(f"Загружено {len(self._transitions)} стейтов, "
                    f"{len(self._global_events)} глобальных событий")

    def register(self, state: BaseState) -> None:
        """
        Регистрирует один стейт.

        Args:
            state: Экземпляр стейта
        """
        self._states[state.name] = state
        logger.debug(f"Зарегистрирован стейт: {state.name}")

    def register_all(self, states: list[BaseState]) -> None:
        """
        Регистрирует список стейтов.

        Args:
            states: Список экземпляров стейтов
        """
        for state in states:
            self.register(state)
        logger.info(f"Зарегистрировано стейтов: {len(states)}")

    def get_state(self, name: str) -> Optional[BaseState]:
        """
        Получить стейт по имени.

        Args:
            name: Имя стейта (например, "common.start")

        Returns:
            Экземпляр стейта или None
        """
        return self._states.get(name)

    def get_user_state(self, user) -> str:
        """
        Определить текущий стейт пользователя.

        Args:
            user: Объект пользователя из БД

        Returns:
            Имя текущего стейта
        """
        # Получаем из БД или используем дефолтный
        if isinstance(user, dict):
            state_name = user.get('current_state')
        else:
            state_name = getattr(user, 'current_state', None)

        return state_name or self._default_state

    def get_next_state(self, current_state: str, event: str) -> Optional[str]:
        """
        Определить следующий стейт по событию.

        Args:
            current_state: Текущий стейт
            event: Событие (возвращаемое из handle)

        Returns:
            Имя следующего стейта или None если переход не определён
        """
        state_config = self._transitions.get(current_state, {})
        events = state_config.get('events', {})

        next_state = events.get(event)

        # Специальные значения
        if next_state == '_same':
            return current_state
        if next_state == '_previous':
            # TODO: Реализовать историю стейтов
            return current_state

        return next_state

    def check_global_event(self, message_text: str, current_state: str) -> Optional[str]:
        """
        Проверить, не является ли сообщение глобальным событием.

        Args:
            message_text: Текст сообщения
            current_state: Текущий стейт

        Returns:
            Имя целевого стейта или None
        """
        # Получаем allow_global для текущего стейта
        state_config = self._transitions.get(current_state, {})
        allowed = state_config.get('allow_global', [])

        for event_name, event_config in self._global_events.items():
            if event_name not in allowed:
                continue

            trigger = event_config.get('trigger', '')
            target = event_config.get('target')

            # Проверяем триггер
            if message_text.startswith(trigger):
                logger.info(f"Глобальное событие: {event_name} -> {target}")
                return target

        return None

    async def handle(self, user, message) -> None:
        """
        Основной метод обработки сообщения.

        1. Определяет текущий стейт пользователя
        2. Проверяет глобальные события
        3. Вызывает handle() текущего стейта
        4. Выполняет переход если нужно

        Args:
            user: Объект пользователя
            message: Telegram Message
        """
        current_state_name = self.get_user_state(user)
        current_state = self.get_state(current_state_name)

        if not current_state:
            logger.error(f"Стейт не найден: {current_state_name}")
            current_state = self.get_state(self._default_state)
            if not current_state:
                logger.error("Даже дефолтный стейт не найден!")
                return

        # Проверяем глобальные события
        message_text = message.text or ''
        global_target = self.check_global_event(message_text, current_state_name)

        if global_target:
            await self._transition(user, current_state, global_target)
            return

        # Обрабатываем в текущем стейте
        try:
            event = await current_state.handle(user, message)
        except Exception as e:
            logger.error(f"Ошибка в стейте {current_state_name}: {e}")
            event = "error"

        # Если есть событие — переходим
        if event:
            next_state_name = self.get_next_state(current_state_name, event)
            if next_state_name and next_state_name != current_state_name:
                await self._transition(user, current_state, next_state_name)

    async def _transition(self, user, from_state: BaseState, to_state_name: str, context: dict = None) -> None:
        """
        Выполнить переход между стейтами.

        Args:
            user: Объект пользователя
            from_state: Текущий стейт
            to_state_name: Имя нового стейта
            context: Дополнительный контекст
        """
        to_state = self.get_state(to_state_name)
        if not to_state:
            logger.error(f"Целевой стейт не найден: {to_state_name}")
            return

        logger.info(f"Переход: {from_state.name} -> {to_state_name}")

        # Выход из текущего стейта
        exit_context = await from_state.exit(user)

        # Объединяем контексты
        full_context = {**(context or {}), **exit_context}

        # Сохраняем новый стейт в БД
        try:
            from db.queries import update_user_state
            chat_id = user.get('chat_id') if isinstance(user, dict) else getattr(user, 'chat_id', None)
            if chat_id:
                await update_user_state(chat_id, to_state_name)
        except Exception as e:
            logger.warning(f"Не удалось сохранить стейт в БД: {e}")

        # Вход в новый стейт
        await to_state.enter(user, full_context)

    async def start(self, user, context: dict = None) -> None:
        """
        Запустить машину для нового пользователя.

        Args:
            user: Объект пользователя
            context: Начальный контекст
        """
        start_state = self.get_state(self._default_state)
        if start_state:
            # Сохраняем начальный стейт в БД
            try:
                from db.queries import update_user_state
                chat_id = user.get('chat_id') if isinstance(user, dict) else getattr(user, 'chat_id', None)
                if chat_id:
                    await update_user_state(chat_id, self._default_state)
            except Exception as e:
                logger.warning(f"Не удалось сохранить начальный стейт: {e}")

            await start_state.enter(user, context)
        else:
            logger.error(f"Стартовый стейт не найден: {self._default_state}")
