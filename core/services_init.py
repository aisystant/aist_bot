"""
Регистрация всех сервисов в реестре.

Добавление нового сервиса:
1. Создать ServiceDescriptor здесь
2. Меню обновится автоматически

Это единственный файл, который нужно менять при добавлении нового сервиса.

Категории (Layer 2 — Pack DP.AISYS.014):
- "scenario": пользовательские сервисы (обучение, прогресс, тест)
- "system": профиль + настройки системы
"""

from core.services import ServiceDescriptor
from core.registry import registry


def register_all_services() -> None:
    """Регистрирует все сервисы бота."""

    # --- SCENARIO: пользовательские сервисы ---

    registry.register(ServiceDescriptor(
        id="marathon",
        i18n_key="service.marathon",
        icon="\U0001f4da",  # 📚
        entry_state="workshop.marathon.lesson",
        category="scenario",
        order=10,
        command="/learn",
        requires_onboarding=True,
    ))

    registry.register(ServiceDescriptor(
        id="feed",
        i18n_key="service.feed",
        icon="\U0001f4d6",  # 📖
        entry_state="feed.topics",
        category="scenario",
        order=20,
        command="/feed",
        requires_onboarding=True,
    ))

    registry.register(ServiceDescriptor(
        id="progress",
        i18n_key="service.progress",
        icon="\U0001f4ca",  # 📊
        entry_state="utility.progress",
        category="scenario",
        order=30,
        command="/progress",
    ))

    registry.register(ServiceDescriptor(
        id="assessment",
        i18n_key="service.assessment",
        icon="\U0001f9ea",  # 🧪
        entry_state="workshop.assessment.flow",
        category="scenario",
        order=40,
        command="/test",
        commands=["/assessment"],
    ))

    # --- SYSTEM: профиль + настройки ---

    registry.register(ServiceDescriptor(
        id="profile",
        i18n_key="service.profile",
        icon="\U0001f464",  # 👤
        entry_state="common.profile",
        category="system",
        order=10,
        command="/profile",
    ))

    registry.register(ServiceDescriptor(
        id="settings",
        i18n_key="service.settings",
        icon="\u2699\ufe0f",  # ⚙️
        entry_state="common.settings",
        category="system",
        order=20,
        command="/settings",
        commands=["/update"],
    ))

    registry.register(ServiceDescriptor(
        id="plans",
        i18n_key="service.plans",
        icon="\U0001f4cb",  # 📋
        entry_state="common.plans",
        category="scenario",
        order=25,
        command="/plan",
        commands=["/rp", "/report"],
    ))

    # --- HIDDEN: в разработке (visible=False, команды работают) ---

    registry.register(ServiceDescriptor(
        id="notes",
        i18n_key="service.notes",
        icon="\U0001f4dd",  # 📝
        entry_state="utility.notes",
        category="scenario",
        order=35,
        visible=False,
    ))

    registry.register(ServiceDescriptor(
        id="twin",
        i18n_key="service.twin",
        icon="\U0001f916",  # 🤖
        entry_state="common.mode_select",  # TODO: twin state
        category="system",
        order=30,
        command="/twin",
        visible=False,
    ))
