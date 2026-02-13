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
        # История предыдущих стейтов: chat_id -> previous_state_name
        self._previous_states: dict[int, str] = {}

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

    def get_next_state(self, current_state: str, event: str, chat_id: int = None) -> Optional[str]:
        """
        Определить следующий стейт по событию.

        Args:
            current_state: Текущий стейт
            event: Событие (возвращаемое из handle)
            chat_id: ID чата для получения previous_state

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
            # Возвращаем предыдущий стейт или mode_select по умолчанию
            return self._previous_states.get(chat_id, 'common.mode_select')

        return next_state

    def set_previous_state(self, chat_id: int, state_name: str) -> None:
        """
        Сохранить предыдущий стейт для пользователя.

        Args:
            chat_id: ID чата
            state_name: Имя стейта для сохранения
        """
        self._previous_states[chat_id] = state_name

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

        # Получаем chat_id для отслеживания previous_state
        chat_id = user.get('chat_id') if isinstance(user, dict) else getattr(user, 'chat_id', None)

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
            # Сохраняем текущий стейт как "предыдущий" перед переходом в глобальный
            if chat_id:
                self.set_previous_state(chat_id, current_state_name)
            await self._transition(user, current_state, global_target, context={'question': message_text[1:].strip()})
            return

        # Обрабатываем в текущем стейте
        try:
            event = await current_state.handle(user, message)
        except Exception as e:
            logger.error(f"Ошибка в стейте {current_state_name}: {e}")
            event = "error"

        # Если есть событие — переходим
        if event:
            next_state_name = self.get_next_state(current_state_name, event, chat_id)
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
        chat_id = user.get('chat_id') if isinstance(user, dict) else getattr(user, 'chat_id', None)
        try:
            from db.queries import update_user_state
            if chat_id:
                await update_user_state(chat_id, to_state_name)
        except Exception as e:
            logger.warning(f"Не удалось сохранить стейт в БД: {e}")

        # ВАЖНО: Обновляем user из БД перед входом в новый стейт
        # Это необходимо, потому что предыдущий стейт мог обновить данные в БД
        # (например, current_topic_index), и новый стейт должен работать с актуальными данными
        fresh_user = user
        if chat_id:
            try:
                from db.queries import get_intern
                fresh_user = await get_intern(chat_id)
                if fresh_user:
                    logger.debug(f"[SM] Refreshed user data for transition to {to_state_name}")
                else:
                    fresh_user = user  # Fallback если не удалось получить
            except Exception as e:
                logger.warning(f"Не удалось обновить user из БД: {e}")
                fresh_user = user

        # Тихий возврат из глобального события (консультация, заметки):
        # не вызываем enter(), чтобы не перерисовывать UI предыдущего стейта
        if (full_context.get('consultation_complete') or full_context.get('notes_complete')) and from_state.name != to_state_name:
            logger.info(f"[SM] Silent return to {to_state_name} (skipping enter)")
            return

        # Вход в новый стейт с актуальными данными
        event = await to_state.enter(fresh_user, full_context)

        # Если enter() вернул событие — обрабатываем авто-переход
        if event:
            next_state = self.get_next_state(to_state_name, event, chat_id)
            if next_state and next_state != to_state_name:
                logger.info(f"[SM] Auto-transition from {to_state_name} via event '{event}'")
                await self.go_to(fresh_user, next_state, full_context)

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

    async def handle_callback(self, user, callback) -> None:
        """
        Обработка callback query (нажатие на inline кнопку).

        1. Определяет текущий стейт пользователя
        2. Вызывает handle_callback() текущего стейта
        3. Выполняет переход если нужно

        Args:
            user: Объект пользователя
            callback: Telegram CallbackQuery
        """
        current_state_name = self.get_user_state(user)
        current_state = self.get_state(current_state_name)

        chat_id = user.get('chat_id') if isinstance(user, dict) else getattr(user, 'chat_id', None)

        if not current_state:
            logger.error(f"Стейт не найден для callback: {current_state_name}")
            current_state = self.get_state(self._default_state)
            if not current_state:
                logger.error("Даже дефолтный стейт не найден!")
                return

        # Проверяем, есть ли у стейта метод handle_callback
        if not hasattr(current_state, 'handle_callback'):
            logger.warning(f"Стейт {current_state_name} не имеет handle_callback")
            return

        # Обрабатываем callback
        try:
            event = await current_state.handle_callback(user, callback)
        except Exception as e:
            logger.error(f"Ошибка в handle_callback стейта {current_state_name}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            event = None

        # Если есть событие — переходим
        if event:
            next_state_name = self.get_next_state(current_state_name, event, chat_id)
            if next_state_name and next_state_name != current_state_name:
                await self._transition(user, current_state, next_state_name)

    async def go_to(self, user, state_name: str, context: dict = None) -> None:
        """
        Перейти в указанный стейт (публичный метод для внешних вызовов).

        Используется для программного перехода, например, из команды /learn.

        Args:
            user: Объект пользователя
            state_name: Имя целевого стейта
            context: Дополнительный контекст
        """
        to_state = self.get_state(state_name)
        if not to_state:
            logger.error(f"Целевой стейт не найден: {state_name}")
            return

        # Получаем текущий стейт (для корректного exit)
        current_state_name = self.get_user_state(user)
        current_state = self.get_state(current_state_name)

        chat_id = user.get('chat_id') if isinstance(user, dict) else getattr(user, 'chat_id', None)
        logger.info(f"[SM] go_to: {current_state_name} -> {state_name} for chat_id={chat_id}")

        # Сохраняем предыдущий стейт при входе в модальный стейт (consultation, notes),
        # чтобы _previous корректно работал при возврате (в т.ч. из callback-вызовов)
        _MODAL_STATES = ('common.consultation', 'utility.notes')
        if chat_id and state_name in _MODAL_STATES and current_state_name != state_name:
            self.set_previous_state(chat_id, current_state_name)

        # Выход из текущего стейта (если есть)
        exit_context = {}
        if current_state:
            exit_context = await current_state.exit(user)

        # Объединяем контексты
        full_context = {**(context or {}), **exit_context}

        # Сохраняем новый стейт в БД
        try:
            from db.queries import update_user_state
            if chat_id:
                await update_user_state(chat_id, state_name)
        except Exception as e:
            logger.warning(f"Не удалось сохранить стейт в БД: {e}")

        # ВАЖНО: Обновляем user из БД перед входом в стейт
        # Это необходимо, потому что предыдущий стейт мог обновить данные в БД
        fresh_user = user
        if chat_id:
            try:
                from db.queries import get_intern
                fresh_user = await get_intern(chat_id)
                if fresh_user:
                    logger.debug(f"[SM] Refreshed user data for go_to {state_name}")
                else:
                    fresh_user = user
            except Exception as e:
                logger.warning(f"Не удалось обновить user из БД: {e}")
                fresh_user = user

        # Тихий возврат из глобального события (консультация, заметки):
        # не вызываем enter(), чтобы не перерисовывать UI предыдущего стейта.
        # НО: проверяем только явно переданный context (не exit_context),
        # иначе /learn и другие команды блокируются silent return при выходе из consultation.
        if context and (context.get('consultation_complete') or context.get('notes_complete')) and current_state_name != state_name:
            logger.info(f"[SM] Silent return to {state_name} (skipping enter)")
            return

        # Вход в новый стейт с актуальными данными
        event = await to_state.enter(fresh_user, full_context)

        # Если enter() вернул событие — обрабатываем переход
        if event:
            next_state_name = self.get_next_state(state_name, event, chat_id)
            if next_state_name and next_state_name != state_name:
                logger.info(f"[SM] Auto-transition from {state_name} via event '{event}'")
                await self.go_to(fresh_user, next_state_name, full_context)
