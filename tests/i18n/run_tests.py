#!/usr/bin/env python3
"""
Запуск тестов i18n без pytest

Usage:
    python tests/i18n/run_tests.py
"""

import re
import sys
from pathlib import Path

# Добавляем корень проекта в path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from i18n import t, get_i18n, SUPPORTED_LANGUAGES, detect_language


def test_all_languages_loaded():
    """Все поддерживаемые языки загружены"""
    i18n = get_i18n()
    for lang in SUPPORTED_LANGUAGES:
        assert lang in i18n.translations, f"Language {lang} not loaded"
    return True


def test_completeness():
    """Все языки 100% переведены"""
    i18n = get_i18n()
    stats = i18n.get_stats()

    for lang in SUPPORTED_LANGUAGES:
        s = stats[lang]
        if s['missing'] > 0:
            missing = i18n.get_missing_keys(lang)
            raise AssertionError(f"{lang}: missing {s['missing']} keys: {list(missing)[:5]}")
    return True


def test_placeholders():
    """Плейсхолдеры сохранены во всех языках"""
    i18n = get_i18n()
    ru_trans = i18n.translations.get('ru', {})

    errors = []
    for lang in ['en', 'es']:
        lang_trans = i18n.translations.get(lang, {})
        for key, ru_text in ru_trans.items():
            lang_text = lang_trans.get(key, '')
            if not lang_text:
                continue

            ru_ph = set(re.findall(r'\{(\w+)\}', ru_text))
            lang_ph = set(re.findall(r'\{(\w+)\}', lang_text))

            if ru_ph != lang_ph:
                errors.append(f"{lang}/{key}: expected {ru_ph}, got {lang_ph}")

    if errors:
        raise AssertionError(f"Placeholder errors:\n" + "\n".join(errors[:5]))
    return True


def test_critical_keys():
    """Критические ключи существуют на всех языках"""
    critical = [
        'welcome.greeting', 'welcome.returning',
        'marathon.day_theory', 'marathon.topic_completed',
        'feed.suggested_topics', 'feed.week_progress',
        'buttons.yes', 'buttons.cancel',
        'fsm.new_user_start', 'consultation.thinking',
    ]

    errors = []
    for key in critical:
        for lang in SUPPORTED_LANGUAGES:
            result = t(key, lang)
            if result == key:
                errors.append(f"{lang}/{key}: not found")

    if errors:
        raise AssertionError(f"Missing critical keys:\n" + "\n".join(errors))
    return True


def test_language_detection():
    """Определение языка работает корректно"""
    assert detect_language('ru') == 'ru'
    assert detect_language('en') == 'en'
    assert detect_language('es') == 'es'
    assert detect_language('uk') == 'ru'  # Украинский → Русский
    assert detect_language('pt') == 'es'  # Португальский → Испанский
    assert detect_language(None) == 'ru'  # None → Русский
    assert detect_language('zh') == 'en'  # Неизвестный → Английский
    return True


def test_translation_function():
    """Функция t() работает"""
    assert t('welcome.greeting', 'ru') == 'Привет!'
    assert t('welcome.greeting', 'en') == 'Hello!'
    assert t('welcome.greeting', 'es') == '¡Hola!'

    # Плейсхолдеры
    result = t('welcome.returning', 'ru', name='Тест')
    assert 'Тест' in result

    # Отсутствующий ключ
    result = t('nonexistent.key', 'ru')
    assert result == 'nonexistent.key'

    return True


def main():
    tests = [
        test_all_languages_loaded,
        test_completeness,
        test_placeholders,
        test_critical_keys,
        test_language_detection,
        test_translation_function,
    ]

    print("=" * 50)
    print("i18n Tests")
    print("=" * 50)
    print()

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            print(f"✅ {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"❌ {test.__name__}")
            print(f"   {e}")
            failed += 1
        except Exception as e:
            print(f"💥 {test.__name__}")
            print(f"   {type(e).__name__}: {e}")
            failed += 1

    print()
    print("=" * 50)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 50)

    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
