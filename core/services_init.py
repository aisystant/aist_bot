"""
Регистрация всех сервисов в реестре.

Добавление нового сервиса:
1. Создать ServiceDescriptor здесь
2. Меню обновится автоматически

Это единственный файл, который нужно менять при добавлении нового сервиса.
"""

from core.services import ServiceDescriptor
from core.registry import registry


def register_all_services() -> None:
    """Регистрирует все сервисы бота."""

    # --- MAIN: основные сервисы (главное меню) ---

    registry.register(ServiceDescriptor(
        id="learning",
        i18n_key="service.learning",
        icon="\U0001f4da",  # 📚
        entry_state="workshop.marathon.lesson",
        category="main",
        order=10,
        command="/learn",
        requires_onboarding=True,
        mode_entry_states={
            "marathon": "workshop.marathon.lesson",
            "feed": "feed.topics",
        },
    ))

    registry.register(ServiceDescriptor(
        id="plans",
        i18n_key="service.plans",
        icon="\U0001f4cb",  # 📋
        entry_state="common.mode_select",  # TODO: plans state
        category="main",
        order=20,
        command="/plan",
        commands=["/rp", "/report"],
    ))

    registry.register(ServiceDescriptor(
        id="notes",
        i18n_key="service.notes",
        icon="\U0001f4dd",  # 📝
        entry_state="utility.notes",
        category="main",
        order=30,
    ))

    registry.register(ServiceDescriptor(
        id="progress",
        i18n_key="service.progress",
        icon="\U0001f4ca",  # 📊
        entry_state="utility.progress",
        category="main",
        order=40,
        command="/progress",
    ))

    # --- TOOLS: инструменты ---

    registry.register(ServiceDescriptor(
        id="assessment",
        i18n_key="service.assessment",
        icon="\U0001f9ea",  # 🧪
        entry_state="workshop.assessment.flow",
        category="tools",
        order=10,
        command="/test",
        commands=["/assessment"],
    ))

    registry.register(ServiceDescriptor(
        id="twin",
        i18n_key="service.twin",
        icon="\U0001f916",  # 🤖
        entry_state="common.mode_select",  # TODO: twin state
        category="tools",
        order=20,
        command="/twin",
    ))

    # --- SETTINGS: настройки ---

    registry.register(ServiceDescriptor(
        id="settings",
        i18n_key="service.settings",
        icon="\u2699\ufe0f",  # ⚙️
        entry_state="common.settings",
        category="settings",
        order=10,
        command="/settings",
        commands=["/update"],
    ))
