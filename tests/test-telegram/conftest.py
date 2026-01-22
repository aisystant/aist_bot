"""
Pytest fixtures для E2E тестирования AIST Track Bot.

ВАЖНО: Этот файл решает проблему совместимости Telethon с pytest-asyncio.
Telethon требует один и тот же event loop, но pytest-asyncio создаёт новый loop
для каждого теста. Решение: синхронные тесты с async wrapper.

Переменные окружения:
- TEST_API_ID: Telegram API ID (https://my.telegram.org)
- TEST_API_HASH: Telegram API Hash
- TEST_BOT_USERNAME: Username бота для тестирования
- TEST_SESSION: Имя файла сессии (по умолчанию 'e2e_test_session')
"""

import os
import sys

# КРИТИЧНО: nest_asyncio ДО любого импорта asyncio
try:
    import nest_asyncio
    nest_asyncio.apply()
except ImportError:
    print("ERROR: nest_asyncio не установлен! pip install nest_asyncio")
    sys.exit(1)

import asyncio
from typing import Coroutine, TypeVar, Any
import pytest

from .client import BotTestClient, BotTest


# Загружаем переменные окружения
def _load_test_env():
    """Загружает переменные из .env.test"""
    env_file = os.path.join(os.path.dirname(__file__), '..', '..', '.env.test')
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())


_load_test_env()


# ============ SINGLETON EVENT LOOP И CLIENT ============

_session_loop: asyncio.AbstractEventLoop = None
_bot_client: BotTestClient = None
_client_started = False


def get_loop() -> asyncio.AbstractEventLoop:
    """Возвращает единственный event loop для всей сессии"""
    global _session_loop
    if _session_loop is None:
        _session_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_session_loop)
    return _session_loop


T = TypeVar('T')


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """
    Выполняет корутину в session loop.

    Использование в тестах:
        def test_something(bot):
            responses = run_async(bot.command_and_wait('/start'))
            assert responses[0].has_text('привет')
    """
    loop = get_loop()
    return loop.run_until_complete(coro)


def _get_client() -> BotTestClient:
    """Создаёт клиента (lazy)"""
    global _bot_client
    if _bot_client is None:
        _bot_client = BotTestClient(
            api_id=int(os.getenv("TEST_API_ID", "0")),
            api_hash=os.getenv("TEST_API_HASH", ""),
            session_name=os.getenv("TEST_SESSION", "e2e_test_session"),
            bot_username=os.getenv("TEST_BOT_USERNAME", ""),
        )
    return _bot_client


def _ensure_started() -> BotTestClient:
    """Запускает клиента если ещё не запущен"""
    global _client_started

    client = _get_client()

    if not _client_started:
        if not client.api_id or not client.api_hash:
            pytest.skip("TEST_API_ID и TEST_API_HASH не настроены")
        if not client.bot_username:
            pytest.skip("TEST_BOT_USERNAME не настроен")

        run_async(client.start())
        _client_started = True

    return client


# ============ FIXTURES ============

@pytest.fixture(scope="session")
def bot(request) -> BotTestClient:
    """
    Клиент бота для тестов. Использовать с run_async().

    Пример:
        def test_start(bot):
            responses = run_async(bot.command_and_wait('/start'))
    """
    client = _ensure_started()

    def cleanup():
        global _client_started, _session_loop
        if _client_started and _session_loop:
            try:
                _session_loop.run_until_complete(client.stop())
            except Exception:
                pass

    request.addfinalizer(cleanup)
    return client


# Алиас для совместимости с существующими тестами
@pytest.fixture(scope="session")
def bot_client(bot) -> BotTestClient:
    """Алиас для bot (совместимость)"""
    return bot


@pytest.fixture
def fresh_bot(bot) -> BotTestClient:
    """Очищает чат перед тестом"""
    run_async(bot.clear_chat())
    run_async(asyncio.sleep(0.5))
    return bot


# Алиас
@pytest.fixture
def fresh_client(fresh_bot) -> BotTestClient:
    """Алиас для fresh_bot (совместимость)"""
    return fresh_bot


@pytest.fixture
def test_user_data():
    """Тестовые данные пользователя"""
    return {
        'name': 'Тестовый Пользователь',
        'occupation': 'Тестировщик ботов',
        'interests': 'Автоматизация, Python, AI',
    }


# ============ PYTEST HOOKS ============

def pytest_configure(config):
    """Регистрация маркеров"""
    config.addinivalue_line("markers", "onboarding: тесты онбординга (1.x)")
    config.addinivalue_line("markers", "marathon: тесты Марафона (2.x)")
    config.addinivalue_line("markers", "feed: тесты Ленты (3.x)")
    config.addinivalue_line("markers", "critical: критические сценарии")
    config.addinivalue_line("markers", "slow: медленные тесты")


# ============ ASSERTIONS HELPER ============

class Assertions:
    """Хелперы для проверок"""

    @staticmethod
    def response_contains(responses, text: str, msg: str = None):
        for r in responses:
            if r.has_text(text):
                return True
        raise AssertionError(
            msg or f"Ни один ответ не содержит '{text}'. "
            f"Ответы: {[r.text[:100] for r in responses]}"
        )

    @staticmethod
    def response_has_button(responses, button_text: str, msg: str = None):
        for r in responses:
            if r.has_button(button_text):
                return True
        raise AssertionError(msg or f"Нет кнопки '{button_text}'")


@pytest.fixture
def assertions():
    return Assertions()
