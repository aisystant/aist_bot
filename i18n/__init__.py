"""
Модуль локализации AIST Bot

Архитектура:
- schema.yaml: мастер-файл с русским + английским + метаданными
- translations/*.yaml: переводы на другие языки (es, fr, de...)

Использование:
    from i18n import t, detect_language, get_language_name

    # Получить перевод
    message = t('welcome.greeting', 'ru')
    message = t('marathon.day_theory', 'en', day=5)

    # Определить язык по коду Telegram
    lang = detect_language(user.language_code)

    # Получить название языка
    name = get_language_name('ru')  # → "Русский"

CLI:
    # Проверка полноты переводов
    python -m i18n.checker check
    python -m i18n.checker check --lang es

    # Экспорт для переводчика
    python -m i18n.checker export --lang fr --format csv

    # Импорт перевода
    python -m i18n.checker import --file fr_translations.csv --lang fr
"""

from .loader import (
    t,
    detect_language,
    get_language_name,
    get_i18n,
    reload,
    I18n,
    SUPPORTED_LANGUAGES,
)

__all__ = [
    't',
    'detect_language',
    'get_language_name',
    'get_i18n',
    'reload',
    'I18n',
    'SUPPORTED_LANGUAGES',
]
