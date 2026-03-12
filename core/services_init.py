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


def _check_access(service_id: str):
    """Создать access_check callback для сервиса."""
    async def check(user_id: int) -> bool:
        from core.access import access_layer
        return await access_layer.has_access(user_id, service_id)
    return check


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
        access_check=_check_access("feed"),
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
    ))

    registry.register(ServiceDescriptor(
        id="mydata",
        i18n_key="service.mydata",
        icon="\U0001f4be",  # 💾
        entry_state="utility.mydata",
        category="system",
        order=15,
        command="/mydata",
    ))

    registry.register(ServiceDescriptor(
        id="feedback",
        i18n_key="service.feedback",
        icon="\U0001f4e3",  # 📣
        entry_state="utility.feedback",
        category="system",
        order=18,
        command="/feedback",
    ))

    registry.register(ServiceDescriptor(
        id="plans",
        i18n_key="service.plans",
        icon="\U0001f4cb",  # 📋
        entry_state="common.plans",
        category="scenario",
        order=35,
        command="/plan",
        commands=["/rp", "/report"],
        access_check=_check_access("plans"),
    ))

    # --- HIDDEN: в разработке ---
    # notes: стейт utility.notes ещё не реализован (Week 8).
    # НЕ регистрировать до создания — иначе machine.go_to() падает с "стейт не найден".
    # См. error_logs #454 (2026-02-22).

    # twin: no SM state — handled directly by handlers/twin.py (twin_router)
    # Do NOT register command="/twin" here, it would intercept and route to SM
