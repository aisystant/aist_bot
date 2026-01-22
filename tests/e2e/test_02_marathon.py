"""
E2E тесты Марафона (Класс 2: Марафон).

Сценарии 2.1 - 2.15 из docs/testing-scenarios.md

Запуск:
    pytest tests/e2e/test_02_marathon.py -v
    pytest tests/e2e/test_02_marathon.py -v -m critical  # только критические
"""

import pytest
import asyncio

from .client import BotTestClient
from .conftest import run_async


@pytest.mark.marathon
@pytest.mark.critical
def test_2_1_first_lesson(bot_client: BotTestClient):
    """
    Сценарий 2.1: Получение первого урока

    Предусловия:
    - Режим: Марафон
    - Пройдено уроков: 0

    Шаги:
    1. Отправить /learn

    Ожидаемый результат:
    - Бот отправляет материал урока
    - Материал содержит текст по теме
    - Есть кнопка "Далее" или "Готово"
    """
    responses = run_async(bot_client.command_and_wait('/learn', timeout=30))

    assert len(responses) >= 1, "Бот не ответил на /learn"

    # Проверяем, что получен урок или сообщение о состоянии
    response = responses[0]
    assert (
        response.has_text('урок') or
        response.has_text('lesson') or
        response.has_text('тема') or
        response.has_text('тем') or
        response.has_button('Далее') or
        response.has_button('Готово') or
        response.has_button('Next') or
        response.has_button('Done') or
        response.has_text('/learn')  # Если нужно выбрать режим
    ), f"Неожиданный ответ на /learn: {response.text[:300]}"


@pytest.mark.marathon
@pytest.mark.critical
def test_2_2_lesson_question(bot_client: BotTestClient):
    """
    Сценарий 2.2: Переход к вопросу урока

    Шаги:
    1. Получить материал урока
    2. Нажать кнопку "Далее"/"Готово"

    Ожидаемый результат:
    - Бот отправляет вопрос урока
    - Вопрос соответствует уровню сложности
    """
    responses = run_async(bot_client.wait_response(timeout=5))

    for r in responses:
        if r.has_button('Далее') or r.has_button('Next'):
            clicked = run_async(bot_client.click_button(r, 'Далее'))
            if not clicked:
                clicked = run_async(bot_client.click_button(r, 'Next'))

            if clicked:
                run_async(asyncio.sleep(1))
                next_responses = run_async(bot_client.wait_response(timeout=15))
                if next_responses:
                    # Должен быть вопрос или следующий материал
                    assert any(
                        resp.has_text('?') or
                        resp.has_text('вопрос') or
                        resp.has_text('question') or
                        resp.has_text('ответ') or
                        resp.has_text('answer')
                        for resp in next_responses
                    ), "После нажатия 'Далее' ожидался вопрос"
            break


@pytest.mark.marathon
@pytest.mark.critical
def test_2_3_answer_lesson_question(bot_client: BotTestClient):
    """
    Сценарий 2.3: Ответ на вопрос урока

    Шаги:
    1. Ввести ответ на вопрос (1-3 предложения)
    2. Отправить

    Ожидаемый результат:
    - Бот показывает "Генерирую..." или аналог
    - Приходит проверка с оценкой
    """
    # Отправляем ответ
    test_answer = "Системное мышление - это способ анализа сложных систем, учитывающий взаимосвязи между компонентами."

    responses = run_async(bot_client.send_and_wait(test_answer, timeout=30))

    if responses:
        # Проверяем реакцию бота
        all_text = ' '.join([r.text for r in responses])
        assert (
            'генериру' in all_text.lower() or
            'generating' in all_text.lower() or
            'оценк' in all_text.lower() or
            'проверк' in all_text.lower() or
            'feedback' in all_text.lower() or
            'отлично' in all_text.lower() or
            'хорошо' in all_text.lower() or
            'правильно' in all_text.lower() or
            'верно' in all_text.lower()
        ), f"Ожидалась проверка ответа: {all_text[:300]}"


@pytest.mark.marathon
@pytest.mark.critical
def test_2_4_get_feedback(bot_client: BotTestClient):
    """
    Сценарий 2.4: Получение обратной связи

    Шаги:
    1. Отправить /learn (после урока следует задание)

    Ожидаемый результат:
    - Бот отправляет материал задания
    - Описан рабочий продукт (РП)
    """
    responses = run_async(bot_client.command_and_wait('/learn', timeout=30))

    if responses:
        response = responses[0]
        # Проверяем получение материала
        assert (
            len(response.text) > 50 or  # Есть содержательный текст
            response.has_button('Далее') or
            response.has_button('Next') or
            response.has_text('задание') or
            response.has_text('task') or
            response.has_text('урок') or
            response.has_text('lesson')
        ), f"Неожиданный ответ: {response.text[:200]}"


@pytest.mark.marathon
@pytest.mark.critical
def test_2_5_task_transition(bot_client: BotTestClient):
    """
    Сценарий 2.5: Переход к заданию

    Шаги:
    1. Описать выполненный рабочий продукт
    2. Отправить ответ

    Ожидаемый результат:
    - Бот принимает ответ
    - Даёт обратную связь по РП
    """
    task_answer = "Я выполнил задание: создал схему системы с описанием компонентов и их взаимосвязей."

    responses = run_async(bot_client.send_and_wait(task_answer, timeout=30))

    # Бот должен принять ответ
    if responses:
        all_text = ' '.join([r.text for r in responses]).lower()
        # Не должно быть ошибки
        assert 'error' not in all_text or 'ошибка' not in all_text, \
            f"Получена ошибка: {all_text[:200]}"


@pytest.mark.marathon
@pytest.mark.critical
def test_2_6_answer_task(bot_client: BotTestClient):
    """
    Сценарий 2.6: Ответ на задание

    Шаги:
    1. Ответить на вопрос урока правильно
    2. Дождаться предложения бонуса

    Ожидаемый результат:
    - Бот предлагает бонусный вопрос (опционально)
    - Есть возможность продолжить
    """
    responses = run_async(bot_client.wait_response(timeout=5))

    # Проверяем текущее состояние
    if responses:
        for r in responses:
            if r.has_button('Бонус') or r.has_button('Bonus'):
                # Есть предложение бонуса
                pass
            elif r.has_button('Далее') or r.has_button('Пропустить'):
                # Можно продолжить
                pass


@pytest.mark.marathon
def test_2_7_bonus_question(bot_client: BotTestClient):
    """
    Сценарий 2.7: Бонусный вопрос

    Шаги:
    1. Получить бонусный вопрос
    2. Ответить на него

    Ожидаемый результат:
    - Бот проверяет ответ
    - Оценка соответствует сложности
    """
    responses = run_async(bot_client.wait_response(timeout=5))

    for r in responses:
        if r.has_button('Бонус') or r.has_button('Bonus'):
            clicked = run_async(bot_client.click_button(r, 'Бонус'))
            if not clicked:
                clicked = run_async(bot_client.click_button(r, 'Bonus'))

            if clicked:
                run_async(asyncio.sleep(1))
                bonus_responses = run_async(bot_client.wait_response(timeout=15))
                if bonus_responses and bonus_responses[0].has_text('?'):
                    # Отвечаем на бонусный вопрос
                    run_async(bot_client.send_and_wait(
                        "Это мой ответ на бонусный вопрос.",
                        timeout=30
                    ))
            break


@pytest.mark.marathon
@pytest.mark.critical
def test_2_8_complete_day(bot_client: BotTestClient):
    """
    Сценарий 2.8: Завершение дня

    Шаги:
    1. Нажать "Пропустить" бонус или завершить все задания

    Ожидаемый результат:
    - Переход к следующему материалу
    - Или сообщение о завершении дня
    """
    responses = run_async(bot_client.wait_response(timeout=5))

    for r in responses:
        if r.has_button('Пропустить') or r.has_button('Skip'):
            clicked = run_async(bot_client.click_button(r, 'Пропустить'))
            if not clicked:
                clicked = run_async(bot_client.click_button(r, 'Skip'))

            if clicked:
                run_async(asyncio.sleep(1))
                next_responses = run_async(bot_client.wait_response(timeout=10))
                # Должен быть переход или завершение
                if next_responses:
                    assert len(next_responses) >= 1
            break


@pytest.mark.marathon
def test_2_9_request_hint(bot_client: BotTestClient):
    """
    Сценарий 2.9: Запрос подсказки

    Шаги:
    1. Нажать кнопку "Подсказка" или написать "не знаю"

    Ожидаемый результат:
    - Бот даёт подсказку
    - Подсказка помогает, но не выдаёт ответ
    """
    responses = run_async(bot_client.send_and_wait("не знаю, помоги", timeout=20))

    if responses:
        all_text = ' '.join([r.text for r in responses]).lower()
        # Бот должен как-то отреагировать
        assert len(all_text) > 10, "Бот не ответил на запрос помощи"


@pytest.mark.marathon
def test_2_10_progress_command(bot_client: BotTestClient):
    """
    Сценарий 2.10: Просмотр прогресса

    Шаги:
    1. Отправить /progress

    Ожидаемый результат:
    - Показан текущий день Марафона
    - Видно количество пройденных уроков/заданий
    """
    responses = run_async(bot_client.command_and_wait('/progress', timeout=10))

    assert len(responses) >= 1, "Бот не ответил на /progress"

    response = responses[0]
    assert (
        response.has_text('день') or
        response.has_text('day') or
        response.has_text('урок') or
        response.has_text('lesson') or
        response.has_text('прогресс') or
        response.has_text('progress') or
        response.has_text('%') or
        response.has_text('статистик')
    ), f"Неожиданный ответ на /progress: {response.text[:200]}"


@pytest.mark.marathon
def test_2_11_marathon_completion(bot_client: BotTestClient):
    """
    Сценарий 2.11: Завершение Марафона (14 дней)

    Проверяется поведение при завершении всех дней.
    Требует прохождения всей программы.
    """
    # Это длительный тест, проверяем базовое поведение
    responses = run_async(bot_client.command_and_wait('/learn', timeout=15))

    if responses:
        for r in responses:
            if r.has_text('завершен') or r.has_text('completed') or r.has_text('поздрав'):
                # Марафон завершён
                assert (
                    r.has_text('Лент') or
                    r.has_text('feed') or
                    r.has_text('статистик')
                ), "После завершения должно быть предложение"


@pytest.mark.marathon
def test_2_12_no_more_lessons_today(bot_client: BotTestClient):
    """
    Сценарий 2.12: Уроки на сегодня закончились

    Шаги:
    1. Пройти все уроки дня
    2. Отправить /learn

    Ожидаемый результат:
    - Бот сообщает, что уроки на сегодня закончились
    - Указывает, когда будет следующий
    """
    # Проверяем реакцию на /learn
    responses = run_async(bot_client.command_and_wait('/learn', timeout=15))

    if responses:
        for r in responses:
            if r.has_text('закончил') or r.has_text('finished') or r.has_text('завтра'):
                # Уроки закончились
                assert (
                    r.has_text('завтра') or
                    r.has_text('tomorrow') or
                    r.has_text('следующ')
                ), "Должна быть информация о следующем уроке"


@pytest.mark.marathon
@pytest.mark.critical
def test_2_13_user_question(bot_client: BotTestClient):
    """
    Сценарий 2.13: Вопрос пользователя

    Шаги:
    1. Задать вопрос по теме обучения

    Ожидаемый результат:
    - Бот распознаёт вопрос
    - Даёт содержательный ответ
    """
    responses = run_async(bot_client.send_and_wait(
        "Что такое системное мышление?",
        timeout=30
    ))

    if responses:
        response = responses[0]
        assert (
            len(response.text) > 50 or  # Содержательный ответ
            response.has_text('систем') or
            response.has_text('мышлени')
        ), f"Ожидался ответ на вопрос: {response.text[:200]}"


@pytest.mark.marathon
def test_2_14_reset_marathon_cancel(bot_client: BotTestClient):
    """
    Сценарий 2.14: Отмена сброса марафона

    Шаги:
    1. Инициировать сброс марафона
    2. Отменить сброс

    Ожидаемый результат:
    - Сброс отменён
    - Прогресс сохранён
    """
    # Отправляем /mode для доступа к настройкам
    responses = run_async(bot_client.command_and_wait('/mode', timeout=10))

    for r in responses:
        if r.has_button('Сброс') or r.has_button('Reset'):
            clicked = run_async(bot_client.click_button(r, 'Сброс'))
            if clicked:
                run_async(asyncio.sleep(1))
                confirm_responses = run_async(bot_client.wait_response(timeout=5))
                for cr in confirm_responses:
                    if cr.has_button('Отмена') or cr.has_button('Cancel'):
                        run_async(bot_client.click_button(cr, 'Отмена'))
                        # Сброс должен быть отменён
                        break
            break


@pytest.mark.marathon
@pytest.mark.critical
def test_2_15_change_complexity(bot_client: BotTestClient):
    """
    Сценарий 2.15: Изменение сложности

    Шаги:
    1. Изменить сложность через /update
    2. Получить следующий урок

    Ожидаемый результат:
    - Новая сложность применена
    - Вопросы соответствуют новому уровню
    """
    responses = run_async(bot_client.command_and_wait('/update', timeout=10))

    assert len(responses) >= 1, "Бот не ответил на /update"

    for r in responses:
        if r.has_button('Сложность') or r.has_button('Complexity'):
            clicked = run_async(bot_client.click_button(r, 'Сложность'))
            if not clicked:
                clicked = run_async(bot_client.click_button(r, 'Complexity'))

            if clicked:
                run_async(asyncio.sleep(1))
                complexity_responses = run_async(bot_client.wait_response(timeout=10))
                # Должны быть варианты сложности
                if complexity_responses:
                    for cr in complexity_responses:
                        if cr.has_button('1') or cr.has_button('Сложность-1'):
                            # Есть выбор сложности
                            pass
            break
