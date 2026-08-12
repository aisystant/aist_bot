"""
Pytest fixtures для E2E тестирования AIST_me_bot.

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
import pytest

# Telethon uses one event loop for the whole E2E session.  Absence of this
# optional E2E dependency must not stop the ordinary test suite at collection.
try:
    import nest_asyncio
except ModuleNotFoundError:
    nest_asyncio = None
else:
    nest_asyncio.apply()

import asyncio
from typing import Any, Coroutine, TypeVar

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

_session_loop: Any = None
_bot_client: Any = None
_client_started = False


def get_loop() -> Any:
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


def _get_client() -> Any:
    """Создаёт клиента (lazy)"""
    global _bot_client
    if _bot_client is None:
        if nest_asyncio is None:
            pytest.skip("Telegram E2E требует nest_asyncio; используйте run_tests.sh")
        from .client import BotTestClient

        _bot_client = BotTestClient(
            api_id=int(os.getenv("TEST_API_ID", "0")),
            api_hash=os.getenv("TEST_API_HASH", ""),
            session_name=os.getenv("TEST_SESSION", "e2e_test_session"),
            bot_username=os.getenv("TEST_BOT_USERNAME", ""),
        )
    return _bot_client


def _ensure_started() -> Any:
    """Запускает клиента если ещё не запущен"""
    global _client_started

    if not _client_started:
        if not os.getenv("TEST_API_ID") or not os.getenv("TEST_API_HASH"):
            pytest.skip("TEST_API_ID и TEST_API_HASH не настроены")
        if not os.getenv("TEST_BOT_USERNAME"):
            pytest.skip("TEST_BOT_USERNAME не настроен")

        client = _get_client()
        run_async(client.start())
        _client_started = True

    return _get_client()


# ============ FIXTURES ============

@pytest.fixture(scope="session")
def bot(request) -> Any:
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
def bot_client(bot) -> Any:
    """Алиас для bot (совместимость)"""
    return bot


@pytest.fixture
def fresh_bot(bot) -> Any:
    """Очищает чат перед тестом"""
    run_async(bot.clear_chat())
    run_async(asyncio.sleep(0.5))
    return bot


# Алиас
@pytest.fixture
def fresh_client(fresh_bot) -> Any:
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
