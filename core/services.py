"""
Сервисный дескриптор — описание одного сервиса в реестре.

Каждый сервис = одна запись в реестре → одна кнопка в меню.
Добавление нового сервиса = добавить ServiceDescriptor в services_init.py.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional, Awaitable


@dataclass
class ServiceDescriptor:
    """Описание сервиса в реестре.

    Attributes:
        id: Уникальный идентификатор ("learning", "plans", "notes", ...)
        i18n_key: Ключ для i18n — используется с t() ("service.learning")
        icon: Эмодзи для кнопки меню
        entry_state: Стейт SM для входа в сервис
        category: Категория для группировки ("main", "tools", "settings")
        order: Порядок в меню (меньше = выше)
        command: Slash-команда ("/learn"), None если нет
        commands: Дополнительные команды для сервиса (e.g. ["/rp", "/plan", "/report"])
        requires_onboarding: Требуется ли пройти онбординг
        feature_flag: Фича-флаг для биллинга (None = всем доступен)
        access_check: Асинхронная функция проверки доступа (user_id) -> bool
        mode_entry_states: Для mode-aware сервисов: {mode: entry_state}
        visible: Показывать ли в меню (False = скрыт, но команды работают)
    """
    id: str
    i18n_key: str
    icon: str
    entry_state: str
    category: str = "main"
    order: int = 100
    command: Optional[str] = None
    commands: list[str] = field(default_factory=list)
    requires_onboarding: bool = False
    feature_flag: Optional[str] = None
    access_check: Optional[Callable[..., Awaitable[bool]]] = None
    mode_entry_states: Optional[dict[str, str]] = None
    visible: bool = True

    def get_entry_state(self, user: dict = None) -> str:
        """Получить entry_state с учётом режима пользователя."""
        if self.mode_entry_states and user:
            mode = user.get('mode', 'marathon') if isinstance(user, dict) else getattr(user, 'mode', 'marathon')
            return self.mode_entry_states.get(mode or 'marathon', self.entry_state)
        return self.entry_state
