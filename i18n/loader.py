"""
Модуль загрузки локализации из YAML-файлов

Архитектура:
- schema.yaml: мастер-файл с русским + английским + метаданными
- translations/*.yaml: переводы на другие языки (es, fr, de...)

Использование:
    from i18n import t

    message = t('welcome.greeting', 'ru')
    message = t('marathon.day_theory', 'en', day=5)
"""

import logging
import re
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# Путь к директории i18n
I18N_DIR = Path(__file__).parent

# Базовые языки (хранятся в schema.yaml)
BASE_LANGUAGES = ['ru', 'en']

# Поддерживаемые языки
SUPPORTED_LANGUAGES = ['ru', 'en', 'es']


class I18n:
    """Система локализации с валидацией и fallback"""

    def __init__(self):
        self.schema: dict[str, Any] = {}
        self.translations: dict[str, dict[str, str]] = {}
        self._load_all()

    def _load_all(self) -> None:
        """Загрузить schema и все переводы"""
        self._load_schema()
        self._load_translations()
        self._validate()

    def _load_schema(self) -> None:
        """Загрузить schema.yaml с базовыми языками"""
        schema_path = I18N_DIR / 'schema.yaml'

        if not schema_path.exists():
            logger.warning(f"Schema file not found: {schema_path}")
            return

        with open(schema_path, 'r', encoding='utf-8') as f:
            self.schema = yaml.safe_load(f) or {}

        # Извлечь переводы ru и en из schema
        for lang in BASE_LANGUAGES:
            self.translations[lang] = {}
            self._extract_translations(self.schema, lang, self.translations[lang])

    def _extract_translations(
        self,
        data: dict,
        lang: str,
        result: dict[str, str],
        prefix: str = ''
    ) -> None:
        """Извлечь переводы для языка из вложенной структуры schema"""
        for key, value in data.items():
            full_key = f"{prefix}{key}" if prefix else key

            if isinstance(value, dict):
                # Если есть ключ языка — это лист с переводом
                if lang in value:
                    translation = value[lang]
                    if translation:  # Не добавляем пустые строки
                        result[full_key] = translation
                else:
                    # Иначе — вложенная структура
                    self._extract_translations(value, lang, result, f"{full_key}.")

    def _load_translations(self) -> None:
        """Загрузить дополнительные переводы из translations/"""
        translations_dir = I18N_DIR / 'translations'

        if not translations_dir.exists():
            return

        for yaml_file in translations_dir.glob('*.yaml'):
            lang = yaml_file.stem  # es.yaml → es

            if lang in BASE_LANGUAGES:
                continue  # ru и en уже загружены из schema

            with open(yaml_file, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}

            self.translations[lang] = {}
            self._flatten_translations(data, self.translations[lang])

    def _flatten_translations(
        self,
        data: dict,
        result: dict[str, str],
        prefix: str = ''
    ) -> None:
        """Преобразовать вложенную структуру в плоский словарь"""
        for key, value in data.items():
            full_key = f"{prefix}{key}" if prefix else key

            if isinstance(value, dict):
                self._flatten_translations(value, result, f"{full_key}.")
            elif isinstance(value, str):
                if value:  # Не добавляем пустые строки
                    result[full_key] = value

    def _validate(self) -> None:
        """Проверить полноту переводов"""
        if not self.translations.get('ru'):
            logger.warning("No Russian translations loaded!")
            return

        ru_keys = set(self.translations['ru'].keys())

        for lang, trans in self.translations.items():
            if lang == 'ru':
                continue

            trans_keys = set(trans.keys())
            missing = ru_keys - trans_keys

            if missing:
                logger.info(
                    f"Language '{lang}': {len(trans_keys)}/{len(ru_keys)} keys "
                    f"({len(missing)} missing)"
                )

    def t(self, key: str, lang: str = 'ru', **kwargs) -> str:
        """
        Получить перевод по ключу

        Args:
            key: ключ перевода (например 'welcome.greeting')
            lang: код языка ('ru', 'en', 'es')
            **kwargs: параметры для форматирования (например name='Иван')

        Returns:
            Переведённая строка или ключ если перевод не найден
        """
        # Получаем перевод для запрошенного языка
        text = self.translations.get(lang, {}).get(key)

        # Fallback на русский
        if text is None:
            text = self.translations.get('ru', {}).get(key)
            if text is not None and lang != 'ru':
                logger.debug(f"Fallback to Russian for key '{key}' (lang={lang})")

        # Если не найден — возвращаем ключ
        if text is None:
            logger.warning(f"Translation not found: '{key}'")
            return key

        # Форматируем с параметрами
        if kwargs:
            try:
                text = text.format(**kwargs)
            except KeyError as e:
                logger.warning(f"Missing placeholder {e} in '{key}'")

        return text

    def get_all_keys(self) -> set[str]:
        """Получить все ключи из schema (русский как источник)"""
        return set(self.translations.get('ru', {}).keys())

    def get_missing_keys(self, lang: str) -> set[str]:
        """Получить недостающие ключи для языка"""
        ru_keys = self.get_all_keys()
        lang_keys = set(self.translations.get(lang, {}).keys())
        return ru_keys - lang_keys

    def get_stats(self) -> dict[str, dict[str, int]]:
        """Получить статистику по языкам"""
        ru_count = len(self.translations.get('ru', {}))

        stats = {}
        for lang in self.translations:
            count = len(self.translations[lang])
            stats[lang] = {
                'translated': count,
                'total': ru_count,
                'missing': ru_count - count if lang != 'ru' else 0
            }

        return stats


# Глобальный экземпляр
_i18n: Optional[I18n] = None


def get_i18n() -> I18n:
    """Получить глобальный экземпляр I18n (lazy loading)"""
    global _i18n
    if _i18n is None:
        _i18n = I18n()
    return _i18n


def t(key: str, lang: str = 'ru', **kwargs) -> str:
    """
    Получить перевод по ключу (короткий алиас)

    Args:
        key: ключ перевода (например 'welcome.greeting')
        lang: код языка ('ru', 'en', 'es')
        **kwargs: параметры для форматирования

    Returns:
        Переведённая строка

    Example:
        t('welcome.greeting', 'ru')
        t('marathon.day_theory', 'en', day=5)
    """
    return get_i18n().t(key, lang, **kwargs)


def detect_language(language_code: Optional[str]) -> str:
    """Определяет язык по коду из Telegram"""
    if not language_code:
        return 'ru'

    code = language_code.lower()[:2]

    if code in SUPPORTED_LANGUAGES:
        return code

    # Маппинг похожих языков
    mapping = {
        'uk': 'ru',  # Украинский → Русский
        'be': 'ru',  # Белорусский → Русский
        'kk': 'ru',  # Казахский → Русский
        'pt': 'es',  # Португальский → Испанский
    }

    return mapping.get(code, 'en')  # По умолчанию английский


def get_language_name(lang: str) -> str:
    """Возвращает название языка"""
    names = {
        'ru': 'Русский',
        'en': 'English',
        'es': 'Español'
    }
    return names.get(lang, lang)


def reload() -> None:
    """Перезагрузить переводы (для hot-reload)"""
    global _i18n
    _i18n = I18n()
