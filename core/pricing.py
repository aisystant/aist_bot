"""
Ценообразование подписки.

Формула:
  Недели 0–4: price = BASE + week × INCREMENT  (линейный рост)
  Недели 5+:  price = ceil5(70 × MULTIPLIER^(week-4))  (экспонента)
  Max: 10,000 Stars (лимит Telegram API)

Grandfathering: Telegram auto-renews подписку по исходной цене.
Новые подписчики платят текущую (растущую) цену.
"""

import math

from config.settings import (
    SUBSCRIPTION_LAUNCH_DATE,
    SUBSCRIPTION_BASE_PRICE,
    SUBSCRIPTION_LINEAR_INCREMENT,
    SUBSCRIPTION_LINEAR_WEEKS,
    SUBSCRIPTION_WEEKLY_MULTIPLIER,
    MAX_SUBSCRIPTION_PRICE,
)
from db.queries.users import moscow_today


def _ceil5(value: float) -> int:
    """Округление ВВЕРХ до кратного 5."""
    return int(math.ceil(value / 5)) * 5


def get_price_at_week(week: int) -> int:
    """Цена подписки на заданной неделе.

    Args:
        week: Номер недели с момента запуска (0 = запуск).

    Returns:
        Цена в Stars.
    """
    if week <= SUBSCRIPTION_LINEAR_WEEKS:
        price = SUBSCRIPTION_BASE_PRICE + week * SUBSCRIPTION_LINEAR_INCREMENT
    else:
        # Цена на конец линейной фазы
        linear_end = SUBSCRIPTION_BASE_PRICE + SUBSCRIPTION_LINEAR_WEEKS * SUBSCRIPTION_LINEAR_INCREMENT
        raw = linear_end * (SUBSCRIPTION_WEEKLY_MULTIPLIER ** (week - SUBSCRIPTION_LINEAR_WEEKS))
        price = _ceil5(raw)
    return min(price, MAX_SUBSCRIPTION_PRICE)


def get_current_price() -> int:
    """Текущая цена подписки в Stars."""
    today = moscow_today()
    days_since_launch = max(0, (today - SUBSCRIPTION_LAUNCH_DATE).days)
    week = days_since_launch // 7
    return get_price_at_week(week)


def get_current_week() -> int:
    """Текущая неделя с момента запуска."""
    today = moscow_today()
    days_since_launch = max(0, (today - SUBSCRIPTION_LAUNCH_DATE).days)
    return days_since_launch // 7
