"""
Интеграции с внешними сервисами.

- llm/ — адаптеры для языковых моделей (Claude)
- telegram/ — клавиатуры и утилиты Telegram
- export/ — экспорт данных (Obsidian, Notion)
"""

from .telegram import keyboards

__all__ = ['keyboards']
