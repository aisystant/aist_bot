"""
pytest конфигурация для i18n тестов
"""

import sys
from pathlib import Path

import pytest

# Добавляем корень проекта в path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session")
def i18n():
    """Фикстура для I18n экземпляра"""
    from i18n import get_i18n
    return get_i18n()


@pytest.fixture(scope="session")
def supported_languages():
    """Список поддерживаемых языков"""
    from i18n import SUPPORTED_LANGUAGES
    return SUPPORTED_LANGUAGES
