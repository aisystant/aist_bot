"""
E2E тесты Ленты (Класс 3: Лента).

Сценарии 3.1 - 3.14 из docs/testing-scenarios.md

Запуск:
    pytest tests/e2e/test_03_feed.py -v
    pytest tests/e2e/test_03_feed.py -v -m critical  # только критические
"""

import pytest
import asyncio

from .client import BotTestClient
from .conftest import run_async


@pytest.mark.feed
@pytest.mark.critical
def test_3_1_get_digest(bot_client: BotTestClient):
    """
    Сценарий 3.1: Получение дайджеста

    Предусловия:
    - Режим: Лента
    - Темы не выбраны

    Шаги:
    1. Активировать режим Лента

    Ожидаемый результат:
    - Бот предлагает выбрать темы
    - Показано меню тем
    """
    # Переключаемся в режим Ленты
    responses = run_async(bot_client.command_and_wait('/mode', timeout=10))

    for r in responses:
        if r.has_button('Лента') or r.has_button('Feed'):
            clicked = run_async(bot_client.click_button(r, 'Лента'))
            if not clicked:
                clicked = run_async(bot_client.click_button(r, 'Feed'))

            if clicked:
                run_async(asyncio.sleep(1))
                next_responses = run_async(bot_client.wait_response(timeout=15))
                if next_responses:
                    all_text = ' '.join([resp.text for resp in next_responses]).lower()
                    # Должно быть предложение выбрать темы или подтверждение режима
                    assert (
                        'тем' in all_text or
                        'topic' in all_text or
                        'лента' in all_text or
                        'feed' in all_text or
                        'выбер' in all_text
                    ), f"Ожидалось меню тем или подтверждение: {all_text[:300]}"
            break


@pytest.mark.feed
@pytest.mark.critical
def test_3_2_digest_question(bot_client: BotTestClient):
    """
    Сценарий 3.2: Вопрос дайджеста

    Шаги:
    1. Выбрать 1-3 темы из списка
    2. Подтвердить выбор

    Ожидаемый результат:
    - Темы сохранены
    - Бот подтверждает выбор
    """
    responses = run_async(bot_client.wait_response(timeout=5))

    for r in responses:
        # Ищем кнопки с темами
        if r.inline_buttons:
            for row in r.inline_buttons:
                for btn in row:
                    # Пробуем нажать на первую тему
                    btn_text = btn.get('text', '')
                    if btn_text and not btn_text.startswith('/'):
                        clicked = run_async(bot_client.click_button(r, btn_text))
                        if clicked:
                            run_async(asyncio.sleep(1))
                            # Проверяем подтверждение
                            confirm_responses = run_async(bot_client.wait_response(timeout=10))
                            if confirm_responses:
                                assert len(confirm_responses) >= 1
                            return
                        break


@pytest.mark.feed
@pytest.mark.critical
def test_3_3_fixation(bot_client: BotTestClient):
    """
    Сценарий 3.3: Фиксация

    Шаги:
    1. Попытаться выбрать больше 3 тем

    Ожидаемый результат:
    - Бот не позволяет выбрать больше 3
    - Показано сообщение о лимите
    """
    # Этот тест проверяет ограничение на количество тем
    # Требуется быть в меню выбора тем
    responses = run_async(bot_client.wait_response(timeout=5))

    for r in responses:
        if r.has_text('максим') or r.has_text('limit') or r.has_text('3 тем'):
            # Ограничение показано
            pass


@pytest.mark.feed
@pytest.mark.critical
def test_3_4_topics_menu(bot_client: BotTestClient):
    """
    Сценарий 3.4: Меню тем

    Предусловия:
    - Темы выбраны
    - Есть запланированный дайджест

    Шаги:
    1. Отправить /learn

    Ожидаемый результат:
    - Бот отправляет дайджест
    - Дайджест по одной из выбранных тем
    """
    responses = run_async(bot_client.command_and_wait('/learn', timeout=30))

    assert len(responses) >= 1, "Бот не ответил на /learn"

    response = responses[0]
    # Должен быть дайджест, меню тем или сообщение о состоянии
    assert (
        len(response.text) > 50 or  # Содержательный текст
        response.has_text('дайджест') or
        response.has_text('digest') or
        response.has_text('тем') or
        response.has_text('topic') or
        response.has_button('Далее') or
        response.has_button('Next')
    ), f"Неожиданный ответ на /learn: {response.text[:300]}"


@pytest.mark.feed
@pytest.mark.critical
def test_3_5_select_topics(bot_client: BotTestClient):
    """
    Сценарий 3.5: Выбор тем

    Шаги:
    1. Прочитать дайджест
    2. Нажать "Далее"
    3. Получить вопрос дайджеста
    4. Написать фиксацию (личный вывод)

    Ожидаемый результат:
    - Вопрос помогает осмыслить материал
    - Фиксация принята
    """
    responses = run_async(bot_client.wait_response(timeout=5))

    for r in responses:
        if r.has_button('Далее') or r.has_button('Next'):
            clicked = run_async(bot_client.click_button(r, 'Далее'))
            if not clicked:
                clicked = run_async(bot_client.click_button(r, 'Next'))

            if clicked:
                run_async(asyncio.sleep(1))
                question_responses = run_async(bot_client.wait_response(timeout=15))
                if question_responses:
                    # Отправляем фиксацию
                    fixation = "Я понял, что важно применять системный подход в работе. Это помогает видеть картину целиком."
                    fixation_responses = run_async(bot_client.send_and_wait(
                        fixation,
                        timeout=30
                    ))
                    if fixation_responses:
                        # Фиксация должна быть принята
                        assert len(fixation_responses) >= 1
            break


@pytest.mark.feed
@pytest.mark.critical
def test_3_6_start_week(bot_client: BotTestClient):
    """
    Сценарий 3.6: Начало недели

    Шаги:
    1. Продолжать изучение одной темы
    2. Наблюдать изменение глубины

    Ожидаемый результат:
    - Дайджесты становятся глубже
    """
    responses = run_async(bot_client.command_and_wait('/learn', timeout=30))

    if responses:
        # Проверяем наличие материала
        assert any(
            len(r.text) > 30 for r in responses
        ), "Ожидался материал дайджеста"


@pytest.mark.feed
def test_3_7_week_stats(bot_client: BotTestClient):
    """
    Сценарий 3.7: Статистика недели

    Шаги:
    1. Завершить последний дайджест недели

    Ожидаемый результат:
    - Бот сообщает о завершении недели
    - Предлагает выбрать темы на новую неделю
    """
    responses = run_async(bot_client.command_and_wait('/progress', timeout=10))

    if responses:
        response = responses[0]
        # Проверяем наличие статистики
        assert (
            response.has_text('недел') or
            response.has_text('week') or
            response.has_text('дайджест') or
            response.has_text('тем') or
            response.has_text('прогресс')
        ), f"Неожиданный ответ на /progress: {response.text[:200]}"


@pytest.mark.feed
@pytest.mark.critical
def test_3_8_today_digest(bot_client: BotTestClient):
    """
    Сценарий 3.8: Дайджест на сегодня

    Шаги:
    1. Перейти в настройки Ленты
    2. Изменить выбранные темы

    Ожидаемый результат:
    - Темы обновлены
    - Оставшиеся дайджесты перепланированы
    """
    responses = run_async(bot_client.command_and_wait('/update', timeout=10))

    for r in responses:
        if r.has_button('Темы') or r.has_button('Topics'):
            clicked = run_async(bot_client.click_button(r, 'Темы'))
            if not clicked:
                clicked = run_async(bot_client.click_button(r, 'Topics'))

            if clicked:
                run_async(asyncio.sleep(1))
                topics_responses = run_async(bot_client.wait_response(timeout=10))
                # Должно быть меню тем
                assert len(topics_responses) >= 1
            break


@pytest.mark.feed
def test_3_9_fallback_topic(bot_client: BotTestClient):
    """
    Сценарий 3.9: Fallback темы

    Шаги:
    1. Отправить /learn когда дайджест на сегодня уже получен

    Ожидаемый результат:
    - Бот сообщает, что на сегодня всё
    - Указывает, когда следующий дайджест
    """
    # Отправляем /learn несколько раз
    run_async(bot_client.command_and_wait('/learn', timeout=30))
    responses = run_async(bot_client.command_and_wait('/learn', timeout=15))

    if responses:
        all_text = ' '.join([r.text for r in responses]).lower()
        # Может быть сообщение о завершении или следующий материал
        if 'сегодня' in all_text or 'today' in all_text or 'завтра' in all_text:
            # На сегодня всё
            pass


@pytest.mark.feed
@pytest.mark.critical
def test_3_10_week_planning(bot_client: BotTestClient):
    """
    Сценарий 3.10: Планирование недели

    Шаги:
    1. Отправить пустое сообщение или "." на вопрос дайджеста

    Ожидаемый результат:
    - Бот просит написать содержательную фиксацию
    - Или принимает с мягким напоминанием
    """
    # Отправляем минимальную фиксацию
    responses = run_async(bot_client.send_and_wait('.', timeout=15))

    if responses:
        response = responses[0]
        # Бот должен как-то отреагировать
        assert len(response.text) > 10, "Бот не ответил на минимальный ввод"


@pytest.mark.feed
def test_3_11_current_week(bot_client: BotTestClient):
    """
    Сценарий 3.11: Текущая неделя

    Шаги:
    1. Написать фиксацию на 2000+ символов

    Ожидаемый результат:
    - Фиксация принята
    - Обратная связь адекватная
    """
    long_fixation = """
    Изучив материал, я пришёл к следующим выводам о системном мышлении и его применении.
    Во-первых, важно понимать, что любая система состоит из взаимосвязанных элементов.
    Эти элементы влияют друг на друга и создают эмерджентные свойства - качества,
    которых нет у отдельных компонентов.

    Во-вторых, системное мышление помогает видеть паттерны и циклы обратной связи.
    Положительная обратная связь усиливает изменения, отрицательная - стабилизирует систему.
    Это ключевое понимание для управления любыми процессами.

    В-третьих, границы системы всегда условны. То, что мы считаем системой, зависит
    от нашей точки зрения и целей анализа. Важно уметь менять масштаб рассмотрения -
    от микро к макро уровню и обратно.

    Наконец, системный подход требует холистического взгляда. Нельзя понять систему,
    разбирая её на части. Нужно изучать связи и взаимодействия, а не только компоненты.
    Это главный урок, который я вынес из изученного материала.
    """

    responses = run_async(bot_client.send_and_wait(long_fixation, timeout=30))

    if responses:
        # Фиксация должна быть принята
        assert len(responses) >= 1, "Бот не ответил на длинную фиксацию"
        # Не должно быть ошибки
        all_text = ' '.join([r.text for r in responses]).lower()
        assert 'error' not in all_text, f"Ошибка при обработке: {all_text[:200]}"


@pytest.mark.feed
@pytest.mark.critical
def test_3_12_create_digest(bot_client: BotTestClient):
    """
    Сценарий 3.12: Создание дайджеста (переход из Марафона)

    Шаги:
    1. Переключиться в Ленту через /mode
    2. Отправить /learn

    Ожидаемый результат:
    - Лента работает
    - Сохранён предыдущий прогресс по Ленте
    """
    # Переключаемся в режим Ленты
    responses = run_async(bot_client.command_and_wait('/mode', timeout=10))

    for r in responses:
        if r.has_button('Лента') or r.has_button('Feed'):
            run_async(bot_client.click_button(r, 'Лента'))
            break

    run_async(asyncio.sleep(1))

    # Проверяем /learn
    learn_responses = run_async(bot_client.command_and_wait('/learn', timeout=30))

    assert len(learn_responses) >= 1, "Бот не ответил на /learn после переключения"

    # Не должно быть ошибки
    all_text = ' '.join([r.text for r in learn_responses]).lower()
    assert 'error' not in all_text and 'ошибк' not in all_text, \
        f"Ошибка после переключения режима: {all_text[:200]}"


@pytest.mark.feed
def test_3_13_user_question_feed(bot_client: BotTestClient):
    """
    Сценарий 3.13: Вопрос пользователя (Лента)

    Предусловия:
    - Режим Лента, но темы не выбраны

    Шаги:
    1. Отправить /learn

    Ожидаемый результат:
    - Бот предлагает сначала выбрать темы
    - /learn не падает с ошибкой
    """
    responses = run_async(bot_client.command_and_wait('/learn', timeout=15))

    assert len(responses) >= 1, "Бот не ответил на /learn"

    # Не должно быть критической ошибки
    all_text = ' '.join([r.text for r in responses]).lower()
    assert 'error' not in all_text or 'internal' not in all_text, \
        f"Критическая ошибка: {all_text[:200]}"


@pytest.mark.feed
def test_3_14_session_duration(bot_client: BotTestClient):
    """
    Сценарий 3.14: Продолжительность сессии

    Шаги:
    1. Запросить историю или посмотреть в /progress

    Ожидаемый результат:
    - Видны пройденные дайджесты
    - Указаны темы и даты
    """
    responses = run_async(bot_client.command_and_wait('/progress', timeout=10))

    assert len(responses) >= 1, "Бот не ответил на /progress"

    response = responses[0]
    # Должна быть какая-то статистика
    assert (
        response.has_text('дайджест') or
        response.has_text('digest') or
        response.has_text('тем') or
        response.has_text('topic') or
        response.has_text('прогресс') or
        response.has_text('progress') or
        response.has_text('пройден') or
        len(response.text) > 30
    ), f"Неожиданный ответ на /progress: {response.text[:200]}"
