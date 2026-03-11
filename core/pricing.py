"""
Ценообразование донатов (Telegram Stars).

Stars = донаты (благодарность), не подписка.
Подписка = «Бесконечное развитие» на Aisystant.
"""

# Фиксированная сумма доната в Stars
DONATION_AMOUNT_STARS = 50


def get_current_price() -> int:
    """Рекомендуемая сумма доната в Stars."""
    return DONATION_AMOUNT_STARS
