"""
Platform Feature Catalog — unified registry of all platform capabilities.

Two categories:
- Bot features: services available in Telegram (status from DB/tier)
- IWE features: external tools requiring local setup (status from tier heuristic)

Each feature has: id, icon, i18n_key, min_tier, status_check, setup_key.
"""

from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from core.tier_config import UITier


@dataclass
class PlatformFeature:
    """One feature in the platform catalog."""
    id: str
    icon: str
    i18n_key: str           # for t(f"features.{i18n_key}.name", lang)
    category: str           # "learning", "productivity", "integration", "automation"
    min_tier: int = 0       # minimum tier to use this feature
    order: int = 100
    # async (chat_id) -> bool | None. None = can't detect (IWE features)
    status_check: Optional[Callable[..., Awaitable[Optional[bool]]]] = None
    # command or action to activate (shown in UI)
    command: Optional[str] = None
    # True if this is an IWE-only feature (not in bot)
    external: bool = False


# ═══════════════════════════════════════════════════════════
# Status check helpers
# ═══════════════════════════════════════════════════════════

async def _check_onboarding(chat_id: int) -> bool:
    from db.queries import get_intern
    intern = await get_intern(chat_id)
    return bool(intern and intern.get('onboarding_completed'))


async def _check_aisystant(chat_id: int) -> bool:
    from db.queries.aisystant import get_aisystant_id
    return await get_aisystant_id(chat_id) is not None


async def _check_subscription(chat_id: int) -> bool:
    from core.tier_detector import detect_ui_tier
    tier = await detect_ui_tier(chat_id)
    return tier >= UITier.T2_LEARNING


async def _check_dt(chat_id: int) -> bool:
    from core.tier_detector import _is_dt_connected
    return await _is_dt_connected(chat_id)


async def _check_github(chat_id: int) -> bool:
    from core.tier_detector import _is_github_connected
    return await _is_github_connected(chat_id)


async def _check_club(chat_id: int) -> bool:
    from db.queries.discourse import get_discourse_account
    return bool(await get_discourse_account(chat_id))


async def _check_wakatime(chat_id: int) -> bool:
    from db.queries.wakatime import get_wakatime_connection
    return bool(await get_wakatime_connection(chat_id))


async def _check_gcal(chat_id: int) -> bool:
    from clients.google_calendar_oauth import google_calendar_oauth
    return await google_calendar_oauth.is_connected(chat_id)


async def _check_always(_chat_id: int) -> bool:
    return True


async def _check_external(_chat_id: int) -> Optional[bool]:
    """Can't detect from bot — return None."""
    return None


# ═══════════════════════════════════════════════════════════
# Feature Catalog
# ═══════════════════════════════════════════════════════════

PLATFORM_FEATURES: list[PlatformFeature] = [
    # --- LEARNING ---
    PlatformFeature(
        id="marathon",
        icon="📚",
        i18n_key="marathon",
        category="learning",
        min_tier=0,
        order=10,
        command="/learn",
        status_check=_check_onboarding,
    ),
    PlatformFeature(
        id="feed",
        icon="📖",
        i18n_key="feed",
        category="learning",
        min_tier=UITier.T2_LEARNING,
        order=20,
        command="/feed",
        status_check=_check_subscription,
    ),
    PlatformFeature(
        id="training",
        icon="🏋️",
        i18n_key="training",
        category="learning",
        min_tier=0,
        order=30,
        command="/train",
        status_check=_check_always,
    ),
    PlatformFeature(
        id="assessment",
        icon="🧪",
        i18n_key="assessment",
        category="learning",
        min_tier=0,
        order=40,
        command="/test",
        status_check=_check_always,
    ),

    # --- PRODUCTIVITY ---
    PlatformFeature(
        id="progress",
        icon="📊",
        i18n_key="progress",
        category="productivity",
        min_tier=0,
        order=10,
        command="/progress",
        status_check=_check_always,
    ),
    PlatformFeature(
        id="plans",
        icon="📋",
        i18n_key="plans",
        category="productivity",
        min_tier=UITier.T2_LEARNING,
        order=20,
        command="/plan",
        status_check=_check_subscription,
    ),
    PlatformFeature(
        id="schedule",
        icon="📅",
        i18n_key="schedule",
        category="productivity",
        min_tier=UITier.T2_LEARNING,
        order=30,
        command="/schedule",
        status_check=_check_subscription,
    ),
    PlatformFeature(
        id="consultation",
        icon="💬",
        i18n_key="consultation",
        category="productivity",
        min_tier=0,
        order=40,
        command="?",
        status_check=_check_always,
    ),
    PlatformFeature(
        id="notes",
        icon="📝",
        i18n_key="notes",
        category="productivity",
        min_tier=UITier.T4_CREATION,
        order=50,
        command=".",
        status_check=_check_github,
    ),

    # --- INTEGRATIONS ---
    PlatformFeature(
        id="aisystant",
        icon="🏛",
        i18n_key="aisystant",
        category="integration",
        min_tier=0,
        order=10,
        command="/link",
        status_check=_check_aisystant,
    ),
    PlatformFeature(
        id="digital_twin",
        icon="🧬",
        i18n_key="digital_twin",
        category="integration",
        min_tier=UITier.T3_PERSONALIZATION,
        order=20,
        command="/twin",
        status_check=_check_dt,
    ),
    PlatformFeature(
        id="github",
        icon="🐙",
        i18n_key="github",
        category="integration",
        min_tier=UITier.T4_CREATION,
        order=30,
        command="/github",
        status_check=_check_github,
    ),
    PlatformFeature(
        id="club",
        icon="🏛",
        i18n_key="club",
        category="integration",
        min_tier=UITier.T2_LEARNING,
        order=40,
        command="/club",
        status_check=_check_club,
    ),
    PlatformFeature(
        id="wakatime",
        icon="⏱",
        i18n_key="wakatime",
        category="integration",
        min_tier=UITier.T4_CREATION,
        order=50,
        status_check=_check_wakatime,
    ),
    PlatformFeature(
        id="gcal",
        icon="📅",
        i18n_key="gcal",
        category="integration",
        min_tier=UITier.T2_LEARNING,
        order=60,
        command="/calendar",
        status_check=_check_gcal,
    ),

    # --- AUTOMATION (IWE / external) ---
    PlatformFeature(
        id="iwe_template",
        icon="🧠",
        i18n_key="iwe_template",
        category="automation",
        min_tier=UITier.T4_CREATION,
        order=10,
        external=True,
        status_check=_check_external,
    ),
    PlatformFeature(
        id="strategist",
        icon="🎯",
        i18n_key="strategist",
        category="automation",
        min_tier=UITier.T4_CREATION,
        order=20,
        external=True,
        status_check=_check_external,
    ),
    PlatformFeature(
        id="pomodoro",
        icon="🍅",
        i18n_key="pomodoro",
        category="automation",
        min_tier=UITier.T4_CREATION,
        order=30,
        external=True,
        status_check=_check_external,
    ),
]


def get_features_by_category() -> dict[str, list[PlatformFeature]]:
    """Group features by category, sorted by order."""
    result: dict[str, list[PlatformFeature]] = {}
    for f in sorted(PLATFORM_FEATURES, key=lambda x: (x.category, x.order)):
        result.setdefault(f.category, []).append(f)
    return result
