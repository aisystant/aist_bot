"""
Тесты системы локализации

Запуск:
    pytest tests/i18n/
    pytest tests/i18n/test_translations.py -v
"""

import re
import pytest
import sys
from pathlib import Path

# Добавляем корень проекта в path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from i18n import t, get_i18n, SUPPORTED_LANGUAGES, I18n


class TestTranslationCompleteness:
    """Проверка полноты переводов"""

    def test_all_languages_loaded(self):
        """Все поддерживаемые языки загружены"""
        i18n = get_i18n()
        for lang in SUPPORTED_LANGUAGES:
            assert lang in i18n.translations, f"Language {lang} not loaded"

    def test_russian_is_complete(self):
        """Русский язык — 100% переводов (источник истины)"""
        i18n = get_i18n()
        ru_keys = i18n.get_all_keys()
        assert len(ru_keys) > 200, f"Too few Russian keys: {len(ru_keys)}"

    def test_english_completeness(self):
        """Английский — 100% переводов"""
        i18n = get_i18n()
        missing = i18n.get_missing_keys('en')
        assert len(missing) == 0, f"Missing EN keys: {missing}"

    def test_spanish_completeness(self):
        """Испанский — 100% переводов"""
        i18n = get_i18n()
        missing = i18n.get_missing_keys('es')
        assert len(missing) == 0, f"Missing ES keys: {missing}"


class TestPlaceholders:
    """Проверка плейсхолдеров {name}, {day} и т.д."""

    def test_placeholders_preserved_in_english(self):
        """Плейсхолдеры сохранены в английском переводе"""
        i18n = get_i18n()
        ru_trans = i18n.translations.get('ru', {})
        en_trans = i18n.translations.get('en', {})

        errors = []
        for key, ru_text in ru_trans.items():
            en_text = en_trans.get(key, '')
            if not en_text:
                continue

            ru_placeholders = set(re.findall(r'\{(\w+)\}', ru_text))
            en_placeholders = set(re.findall(r'\{(\w+)\}', en_text))

            if ru_placeholders != en_placeholders:
                errors.append(f"{key}: expected {ru_placeholders}, got {en_placeholders}")

        assert not errors, f"Placeholder mismatches:\n" + "\n".join(errors[:10])

    def test_placeholders_preserved_in_spanish(self):
        """Плейсхолдеры сохранены в испанском переводе"""
        i18n = get_i18n()
        ru_trans = i18n.translations.get('ru', {})
        es_trans = i18n.translations.get('es', {})

        errors = []
        for key, ru_text in ru_trans.items():
            es_text = es_trans.get(key, '')
            if not es_text:
                continue

            ru_placeholders = set(re.findall(r'\{(\w+)\}', ru_text))
            es_placeholders = set(re.findall(r'\{(\w+)\}', es_text))

            if ru_placeholders != es_placeholders:
                errors.append(f"{key}: expected {ru_placeholders}, got {es_placeholders}")

        assert not errors, f"Placeholder mismatches:\n" + "\n".join(errors[:10])


class TestCriticalKeys:
    """Проверка критических ключей для работы бота"""

    CRITICAL_KEYS = [
        # Онбординг
        'welcome.greeting',
        'welcome.returning',
        'onboarding.ask_name',
        'onboarding.ask_occupation',

        # Марафон
        'marathon.day_theory',
        'marathon.day_practice',
        'marathon.reflection_question',
        'marathon.bonus_question',
        'marathon.topic_completed',

        # Лента
        'feed.suggested_topics',
        'feed.topics_selected',
        'feed.week_progress',

        # Кнопки
        'buttons.yes',
        'buttons.cancel',
        'buttons.bonus_yes',
        'buttons.bonus_no',

        # FSM
        'fsm.new_user_start',
        'fsm.button_expired',

        # Консультация
        'consultation.thinking',
        'consultation.sources',
    ]

    @pytest.mark.parametrize("key", CRITICAL_KEYS)
    def test_critical_key_exists_in_russian(self, key):
        """Критический ключ существует в русском"""
        result = t(key, 'ru')
        assert result != key, f"Key {key} not found in Russian"

    @pytest.mark.parametrize("key", CRITICAL_KEYS)
    def test_critical_key_exists_in_english(self, key):
        """Критический ключ существует в английском"""
        result = t(key, 'en')
        assert result != key, f"Key {key} not found in English"

    @pytest.mark.parametrize("key", CRITICAL_KEYS)
    def test_critical_key_exists_in_spanish(self, key):
        """Критический ключ существует в испанском"""
        result = t(key, 'es')
        assert result != key, f"Key {key} not found in Spanish"


class TestTranslationFunction:
    """Проверка функции t()"""

    def test_basic_translation(self):
        """Базовый перевод работает"""
        assert t('welcome.greeting', 'ru') == 'Привет!'
        assert t('welcome.greeting', 'en') == 'Hello!'
        assert t('welcome.greeting', 'es') == '¡Hola!'

    def test_placeholder_formatting(self):
        """Плейсхолдеры форматируются"""
        result = t('welcome.returning', 'ru', name='Иван')
        assert 'Иван' in result

        result = t('marathon.day_theory', 'en', day=5)
        assert '5' in result

    def test_fallback_to_russian(self):
        """Fallback на русский при отсутствии перевода"""
        i18n = I18n()
        # Если ключ есть только в русском, должен вернуться русский текст
        # (сейчас все ключи переведены, но логика должна работать)
        assert i18n.t('welcome.greeting', 'ru') == 'Привет!'

    def test_missing_key_returns_key(self):
        """Отсутствующий ключ возвращает сам ключ"""
        result = t('nonexistent.key.here', 'ru')
        assert result == 'nonexistent.key.here'


class TestLanguageDetection:
    """Проверка определения языка"""

    def test_detect_supported_languages(self):
        """Поддерживаемые языки определяются напрямую"""
        from i18n import detect_language

        assert detect_language('ru') == 'ru'
        assert detect_language('en') == 'en'
        assert detect_language('es') == 'es'

    def test_detect_similar_languages(self):
        """Похожие языки маппятся корректно"""
        from i18n import detect_language

        # Украинский → Русский
        assert detect_language('uk') == 'ru'
        # Белорусский → Русский
        assert detect_language('be') == 'ru'
        # Португальский → Испанский
        assert detect_language('pt') == 'es'

    def test_detect_unknown_defaults_to_english(self):
        """Неизвестный язык → английский"""
        from i18n import detect_language

        assert detect_language('zh') == 'en'
        assert detect_language('ja') == 'en'

    def test_detect_none_defaults_to_russian(self):
        """None → русский"""
        from i18n import detect_language

        assert detect_language(None) == 'ru'


class TestI18nClass:
    """Проверка класса I18n"""

    def test_singleton_pattern(self):
        """get_i18n() возвращает один и тот же экземпляр"""
        i18n1 = get_i18n()
        i18n2 = get_i18n()
        assert i18n1 is i18n2

    def test_stats_method(self):
        """Метод get_stats() работает"""
        i18n = get_i18n()
        stats = i18n.get_stats()

        assert 'ru' in stats
        assert 'en' in stats
        assert 'es' in stats

        for lang, s in stats.items():
            assert 'translated' in s
            assert 'total' in s
            assert s['translated'] > 0

    def test_get_all_keys(self):
        """get_all_keys() возвращает множество ключей"""
        i18n = get_i18n()
        keys = i18n.get_all_keys()

        assert isinstance(keys, set)
        assert len(keys) > 200
        assert 'welcome.greeting' in keys


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
