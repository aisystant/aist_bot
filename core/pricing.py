"""
Ценообразование донатов (Telegram Stars).

WP-85: Stars = донаты (благодарность), НЕ влияют на тир/доступ.
Подписка = «Инженерия интеллекта» на Aisystant (system-school.ru).
"""

# Фиксированная сумма доната в Stars
DONATION_AMOUNT_STARS = 50


def get_current_price() -> int:
    """Рекомендуемая сумма доната в Stars."""
    return DONATION_AMOUNT_STARS
