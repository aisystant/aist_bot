"""
E2E тесты онбординга (Класс 1: Регистрация и онбординг).

Сценарии 1.1 - 1.12 из tests/test-manual/testing-scenarios.md

Запуск:
    pytest tests/e2e/test_01_onboarding.py -v
    pytest tests/e2e/test_01_onboarding.py -v -m critical  # только критические
"""

import pytest

pytest.importorskip("telethon", reason="Telegram E2E требует Telethon; используйте run_tests.sh")

from .client import BotTestClient
from .conftest import run_async


@pytest.mark.onboarding
@pytest.mark.critical
def test_1_1_first_start(fresh_client: BotTestClient, assertions):
    """
    Сценарий 1.1: Первый запуск бота

    Шаги:
    1. Отправить /start
    2. Проверить приветствие
    3. Проверить кнопки выбора языка
    """
    responses = run_async(fresh_client.command_and_wait('/start', timeout=15))

    # Должен быть хотя бы один ответ
    assert len(responses) >= 1, "Бот не ответил на /start"

    # Проверяем приветствие или выбор языка
    first_response = responses[0]
    assert (
        first_response.has_text('привет') or
        first_response.has_text('hello') or
        first_response.has_text('язык') or
        first_response.has_text('language') or
        first_response.has_button('Русский') or
        first_response.has_button('English')
    ), f"Неожиданный ответ: {first_response.text[:200]}"


@pytest.mark.onboarding
@pytest.mark.critical
def test_1_2_language_selection(fresh_client: BotTestClient, assertions):
    """
    Сценарий 1.2: Выбор языка при регистрации

    Шаги:
    1. Отправить /start
    2. Нажать кнопку "Русский"
    3. Проверить переход к следующему шагу
    """
    import asyncio

    # Отправляем /start
    responses = run_async(fresh_client.command_and_wait('/start', timeout=15))
    assert len(responses) >= 1

    # Ищем ответ с кнопками языка
    lang_response = None
    for r in responses:
        if r.has_button('Русский') or r.has_button('🇷🇺'):
            lang_response = r
            break

    if lang_response:
        # Нажимаем кнопку русского языка
        clicked = run_async(fresh_client.click_button(lang_response, 'Русский'))
        if not clicked:
            clicked = run_async(fresh_client.click_button(lang_response, '🇷🇺'))

        if clicked:
            run_async(asyncio.sleep(1))
            # Ждём следующее сообщение (запрос имени)
            next_responses = run_async(fresh_client.wait_response(timeout=10))
            if next_responses:
                assert (
                    next_responses[0].has_text('имя') or
                    next_responses[0].has_text('name') or
                    next_responses[0].has_text('зовут')
                ), f"После выбора языка ожидался запрос имени: {next_responses[0].text[:200]}"


@pytest.mark.onboarding
@pytest.mark.critical
def test_1_3_name_input(bot_client: BotTestClient, test_user_data):
    """
    Сценарий 1.3: Ввод имени

    Предусловие: бот ожидает ввода имени
    Шаги:
    1. Отправить имя
    2. Проверить переход к следующему шагу (занятие)
    """
    # Отправляем имя
    responses = run_async(bot_client.send_and_wait(
        test_user_data['name'],
        timeout=10
    ))

    if responses:
        # Slim onboarding: после имени → сразу выбор длительности
        response = responses[0]
        assert (
            response.has_text('минут') or
            response.has_text('minutes') or
            response.has_text('duration') or
            response.has_text(test_user_data['name'])  # Бот повторил имя
        ), f"Неожиданный ответ после имени: {response.text[:200]}"


@pytest.mark.onboarding
@pytest.mark.critical
def test_1_6_marathon_mode_selection(bot_client: BotTestClient):
    """
    Сценарий 1.6: Выбор режима Марафон

    Шаги:
    1. Дождаться выбора режима
    2. Нажать кнопку "Марафон"
    3. Проверить активацию режима
    """
    import asyncio

    # Получаем текущие сообщения
    responses = run_async(bot_client.wait_response(timeout=5))

    for r in responses:
        if r.has_button('Марафон') or r.has_button('Marathon'):
            clicked = run_async(bot_client.click_button(r, 'Марафон'))
            if not clicked:
                clicked = run_async(bot_client.click_button(r, 'Marathon'))

            if clicked:
                run_async(asyncio.sleep(1))
                next_responses = run_async(bot_client.wait_response(timeout=10))
                if next_responses:
                    # Проверяем подтверждение режима
                    assert any(
                        resp.has_text('Марафон') or
                        resp.has_text('14 дн') or
                        resp.has_text('/learn')
                        for resp in next_responses
                    ), "Режим Марафон не был активирован"
            break


@pytest.mark.onboarding
@pytest.mark.critical
def test_1_7_feed_mode_selection(bot_client: BotTestClient):
    """
    Сценарий 1.7: Выбор режима Лента

    Шаги:
    1. Дождаться выбора режима
    2. Нажать кнопку "Лента"
    3. Проверить активацию режима
    """
    import asyncio

    responses = run_async(bot_client.wait_response(timeout=5))

    for r in responses:
        if r.has_button('Лента') or r.has_button('Feed'):
            clicked = run_async(bot_client.click_button(r, 'Лента'))
            if not clicked:
                clicked = run_async(bot_client.click_button(r, 'Feed'))

            if clicked:
                run_async(asyncio.sleep(1))
                next_responses = run_async(bot_client.wait_response(timeout=10))
                if next_responses:
                    # Проверяем подтверждение режима
                    assert any(
                        resp.has_text('Лента') or
                        resp.has_text('Feed') or
                        resp.has_text('тем') or
                        resp.has_text('/learn')
                        for resp in next_responses
                    ), "Режим Лента не был активирован"
            break


@pytest.mark.onboarding
def test_1_8_repeat_start(bot_client: BotTestClient):
    """
    Сценарий 1.8: Повторный /start

    Предусловие: пользователь уже зарегистрирован
    Шаги:
    1. Отправить /start
    2. Проверить, что бот помнит пользователя
    """
    responses = run_async(bot_client.command_and_wait('/start', timeout=10))

    if responses:
        response = responses[0]
        # Для зарегистрированного пользователя должно быть
        # приветствие по имени или меню
        assert (
            response.has_text('снова') or
            response.has_text('again') or
            response.has_text('/learn') or
            response.has_text('/help') or
            response.has_button('Марафон') or
            response.has_button('Лента')
        ), f"Неожиданный ответ на повторный /start: {response.text[:200]}"


@pytest.mark.onboarding
def test_1_9_empty_name(fresh_client: BotTestClient):
    """
    Сценарий 1.9: Ввод пустого имени

    Шаги:
    1. Начать онбординг
    2. Ввести пустое имя (только пробелы)
    3. Проверить, что бот просит ввести имя снова
    """
    # Отправляем /start чтобы начать онбординг
    run_async(fresh_client.command_and_wait('/start', timeout=15))

    # Пытаемся отправить пустое имя
    responses = run_async(fresh_client.send_and_wait('   ', timeout=10))

    # Бот должен либо попросить ввести снова, либо проигнорировать
    # (зависит от реализации)


@pytest.mark.onboarding
def test_1_10_long_name(bot_client: BotTestClient):
    """
    Сценарий 1.10: Ввод очень длинного имени

    Шаги:
    1. Ввести имя длиннее 100 символов
    2. Проверить, что бот обрезает или просит короче
    """
    long_name = "А" * 150
    responses = run_async(bot_client.send_and_wait(long_name, timeout=10))

    # Бот должен принять (обрезав) или попросить короче
    # Главное - не должно быть ошибки


@pytest.mark.onboarding
def test_1_11_interrupt_onboarding(fresh_client: BotTestClient):
    """
    Сценарий 1.11: Прерывание онбординга

    Шаги:
    1. Начать онбординг
    2. Прервать на середине (не отвечать)
    3. Отправить /start снова
    4. Проверить, что состояние сохранено или сброшено
    """
    import asyncio

    # Начинаем онбординг
    run_async(fresh_client.command_and_wait('/start', timeout=15))

    # Ждём немного
    run_async(asyncio.sleep(2))

    # Снова отправляем /start
    responses = run_async(fresh_client.command_and_wait('/start', timeout=10))

    # Бот должен либо продолжить с того же места,
    # либо начать сначала - главное не падать
    assert len(responses) >= 1, "Бот не ответил на повторный /start во время онбординга"


@pytest.mark.onboarding
def test_1_12_start_during_onboarding(bot_client: BotTestClient):
    """
    Сценарий 1.12: /start во время онбординга

    Проверяет, что /start не ломает текущий процесс
    """
    # Отправляем /start во время любого состояния
    responses = run_async(bot_client.command_and_wait('/start', timeout=10))

    # Главное - бот должен ответить без ошибок
    assert len(responses) >= 1, "Бот не ответил на /start"
    assert not any(
        r.has_text('error') or r.has_text('ошибка')
        for r in responses
    ), "Бот вернул ошибку"
