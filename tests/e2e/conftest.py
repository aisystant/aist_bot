"""
Pytest fixtures для E2E тестирования AIST Track Bot.

Переменные окружения:
- TEST_API_ID: Telegram API ID (https://my.telegram.org)
- TEST_API_HASH: Telegram API Hash
- TEST_BOT_USERNAME: Username бота для тестирования (например @aist_track_bot)
- TEST_SESSION: Имя файла сессии (по умолчанию 'e2e_test_session')

Первый запуск:
1. Установить зависимости: pip install telethon nest_asyncio pytest-asyncio
2. Создать .env.test с переменными
3. Запустить: pytest tests/e2e/ -v
4. При первом запуске ввести номер телефона и код подтверждения
"""

import os
import sys

# КРИТИЧНО: nest_asyncio должен быть применён ДО любого импорта asyncio
# Это решает проблему "event loop must not change after connection"
try:
    import nest_asyncio
    nest_asyncio.apply()
except ImportError:
    print("ERROR: nest_asyncio не установлен!")
    print("Установите: pip install nest_asyncio")
    sys.exit(1)

import asyncio
import pytest

from .client import BotTestClient, BotTest


# Загружаем переменные окружения для тестов
def _load_test_env():
    """Загружает переменные из .env.test если есть"""
    env_file = os.path.join(os.path.dirname(__file__), '..', '..', '.env.test')
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())


_load_test_env()


# Глобальные переменные для singleton клиента
_bot_client: BotTestClient = None
_client_started = False
_session_loop: asyncio.AbstractEventLoop = None


def pytest_configure(config):
    """Регистрация маркеров и ранняя инициализация"""
    # Применяем nest_asyncio ещё раз на всякий случай
    try:
        import nest_asyncio
        nest_asyncio.apply()
    except ImportError:
        pass

    config.addinivalue_line("markers", "onboarding: тесты онбординга (1.x)")
    config.addinivalue_line("markers", "marathon: тесты Марафона (2.x)")
    config.addinivalue_line("markers", "feed: тесты Ленты (3.x)")
    config.addinivalue_line("markers", "modes: тесты переключения режимов (4.x)")
    config.addinivalue_line("markers", "settings: тесты настроек (5.x)")
    config.addinivalue_line("markers", "language: тесты языка (6.x)")
    config.addinivalue_line("markers", "commands: тесты команд (7.x)")
    config.addinivalue_line("markers", "edge_cases: граничные случаи (8.x)")
    config.addinivalue_line("markers", "slow: медленные тесты (требуют генерации AI)")
    config.addinivalue_line("markers", "critical: критические сценарии")


def get_session_loop() -> asyncio.AbstractEventLoop:
    """Получает или создаёт единый event loop для сессии"""
    global _session_loop
    if _session_loop is None:
        try:
            _session_loop = asyncio.get_event_loop()
        except RuntimeError:
            _session_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_session_loop)
    return _session_loop


@pytest.fixture(scope="session")
def event_loop():
    """Единый event loop для всей сессии тестов"""
    loop = get_session_loop()
    yield loop
    # Не закрываем - nest_asyncio может использовать


def get_or_create_client() -> BotTestClient:
    """Получает или создаёт глобальный клиент"""
    global _bot_client

    if _bot_client is None:
        _bot_client = BotTestClient(
            api_id=int(os.getenv("TEST_API_ID", "0")),
            api_hash=os.getenv("TEST_API_HASH", ""),
            session_name=os.getenv("TEST_SESSION", "e2e_test_session"),
            bot_username=os.getenv("TEST_BOT_USERNAME", ""),
        )
    return _bot_client


def start_client_sync():
    """Синхронно запускает клиент в session loop"""
    global _client_started

    if _client_started:
        return

    client = get_or_create_client()

    # Проверяем конфигурацию
    if not client.api_id or not client.api_hash:
        pytest.skip("TEST_API_ID и TEST_API_HASH не настроены")

    if not client.bot_username:
        pytest.skip("TEST_BOT_USERNAME не настроен")

    loop = get_session_loop()
    loop.run_until_complete(client.start())
    _client_started = True


@pytest.fixture(scope="session")
def bot_client(event_loop) -> BotTestClient:
    """
    Фикстура клиента бота на всю сессию тестов.
    Клиент запускается синхронно в session loop.
    """
    start_client_sync()
    return get_or_create_client()


@pytest.fixture
async def fresh_client(bot_client: BotTestClient) -> BotTestClient:
    """Фикстура для чистого теста - очищает чат"""
    await bot_client.clear_chat()
    await asyncio.sleep(0.5)
    return bot_client


@pytest.fixture
def test_user_data():
    """Тестовые данные пользователя для онбординга"""
    return {
        'name': 'Тестовый Пользователь',
        'occupation': 'Тестировщик ботов',
        'interests': 'Автоматизация, Python, AI',
        'motivation': 'Хочу научиться системному мышлению',
        'goals': 'Стать лучшим специалистом',
        'study_duration': '15',
    }


def pytest_sessionfinish(session, exitstatus):
    """Очистка при завершении сессии тестов"""
    global _bot_client, _client_started, _session_loop

    if _bot_client and _client_started and _session_loop:
        try:
            _session_loop.run_until_complete(_bot_client.stop())
        except Exception:
            pass


# Хелперы для тестов
class Assertions:
    """Хелперы для ассертов в E2E тестах"""

    @staticmethod
    def response_contains(responses, text: str, msg: str = None):
        """Проверяет, что хотя бы один ответ содержит текст"""
        for r in responses:
            if r.has_text(text):
                return True
        raise AssertionError(
            msg or f"Ни один ответ не содержит '{text}'. "
            f"Полученные ответы: {[r.text[:100] for r in responses]}"
        )

    @staticmethod
    def response_has_button(responses, button_text: str, msg: str = None):
        """Проверяет наличие кнопки"""
        for r in responses:
            if r.has_button(button_text):
                return True
        raise AssertionError(
            msg or f"Ни один ответ не содержит кнопку '{button_text}'"
        )

    @staticmethod
    def response_count(responses, expected: int, msg: str = None):
        """Проверяет количество ответов"""
        if len(responses) != expected:
            raise AssertionError(
                msg or f"Ожидалось {expected} ответов, получено {len(responses)}"
            )


@pytest.fixture
def assertions():
    """Фикстура с хелперами для ассертов"""
    return Assertions()
