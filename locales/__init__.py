"""
Модуль локализации для AI System Track бота.
Поддерживает русский (ru), английский (en) и испанский (es) языки.
"""

import yaml
from pathlib import Path
from typing import Any

# Загружаем переводы при импорте модуля
_translations: dict[str, dict] = {}
_locales_dir = Path(__file__).parent

SUPPORTED_LANGUAGES = ['ru', 'en', 'es']
DEFAULT_LANGUAGE = 'ru'
FALLBACK_CHAIN = {'es': 'en', 'en': 'ru', 'ru': 'ru'}


def _load_translations():
    """Загрузить все файлы переводов"""
    global _translations
    for lang in SUPPORTED_LANGUAGES:
        file_path = _locales_dir / f"{lang}.yaml"
        if file_path.exists():
            with open(file_path, 'r', encoding='utf-8') as f:
                _translations[lang] = yaml.safe_load(f) or {}
        else:
            _translations[lang] = {}


def _get_nested(data: dict, keys: list[str]) -> Any:
    """Получить вложенное значение по списку ключей"""
    for key in keys:
        if isinstance(data, dict) and key in data:
            data = data[key]
        else:
            return None
    return data


def t(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """
    Получить перевод по ключу.

    Args:
        key: Ключ перевода (например, 'onboarding.ask_name')
        lang: Код языка ('ru', 'en', 'es')
        **kwargs: Переменные для подстановки в строку

    Returns:
        Переведённая строка или ключ, если перевод не найден

    Example:
        t('onboarding.ask_name', 'en')  # "What is your name?"
        t('progress.day', 'ru', n=5)    # "День 5"
    """
    if not _translations:
        _load_translations()

    # Нормализуем язык
    lang = lang[:2].lower() if lang else DEFAULT_LANGUAGE
    if lang not in SUPPORTED_LANGUAGES:
        lang = DEFAULT_LANGUAGE

    keys = key.split('.')

    # Пробуем получить перевод с fallback
    current_lang = lang
    while current_lang:
        value = _get_nested(_translations.get(current_lang, {}), keys)
        if value is not None:
            # Подставляем переменные
            if kwargs and isinstance(value, str):
                try:
                    return value.format(**kwargs)
                except KeyError:
                    return value
            return value
        # Переходим к fallback языку
        current_lang = FALLBACK_CHAIN.get(current_lang)
        if current_lang == lang:
            break

    # Перевод не найден - возвращаем ключ
    return key


def get_language_name(lang: str) -> str:
    """Получить название языка на этом же языке"""
    names = {
        'ru': '🇷🇺 Русский',
        'en': '🇬🇧 English',
        'es': '🇪🇸 Español'
    }
    return names.get(lang, lang)


def detect_language(language_code: str | None) -> str:
    """
    Определить язык по коду из Telegram.

    Args:
        language_code: Код языка из message.from_user.language_code

    Returns:
        Поддерживаемый код языка или DEFAULT_LANGUAGE
    """
    if not language_code:
        return DEFAULT_LANGUAGE

    lang = language_code[:2].lower()
    if lang in SUPPORTED_LANGUAGES:
        return lang

    return DEFAULT_LANGUAGE


# Загружаем переводы при импорте
_load_translations()
