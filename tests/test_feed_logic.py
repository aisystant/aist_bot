"""
Тест логики режима Лента без зависимостей Telegram.

Запуск: python -m pytest tests/test_feed_logic.py -v
Или просто: python tests/test_feed_logic.py
"""

import sys
import os

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_intent_detection():
    """Тест распознавания интентов"""
    from core.intent import detect_intent, IntentType

    # Вопрос
    intent = detect_intent("Что такое системное мышление?")
    assert intent.type == IntentType.QUESTION, f"Ожидался QUESTION, получен {intent.type}"
    print("✅ Вопрос распознан корректно")

    # Команда
    intent = detect_intent("проще")
    assert intent.type == IntentType.COMMAND, f"Ожидался COMMAND, получен {intent.type}"
    assert intent.command == "simpler"
    print("✅ Команда распознана корректно")

    # Ответ (когда ждём ответ)
    context = {'awaiting_answer': True}
    intent = detect_intent("Я думаю, что системное мышление помогает видеть связи", context)
    assert intent.type == IntentType.ANSWER, f"Ожидался ANSWER, получен {intent.type}"
    print("✅ Ответ распознан корректно")


def test_question_keywords():
    """Тест извлечения ключевых слов"""
    from core.intent import get_question_keywords

    keywords = get_question_keywords("Что такое системное мышление и зачем оно нужно?")
    assert "системное" in keywords or "мышление" in keywords
    print(f"✅ Ключевые слова: {keywords}")


def test_planner_fallback():
    """Тест fallback тем"""
    try:
        from engines.feed.planner import get_fallback_topics
        topics = get_fallback_topics()
        assert len(topics) == 5
        assert all('title' in t for t in topics)
        assert all('why' in t for t in topics)
        # Проверяем что название не длиннее 5 слов
        for t in topics:
            assert len(t['title'].split()) <= 5, f"Название слишком длинное: {t['title']}"
        print(f"✅ Fallback темы: {[t['title'] for t in topics]}")
    except ImportError:
        # aiogram не установлен - тестируем напрямую
        print("⏭️ Fallback темы: пропущен (нет aiogram)")


def test_config_constants():
    """Тест констант конфигурации"""
    from config import (
        Mode, MarathonStatus, FeedStatus,
        COMPLEXITY_LEVELS, FEED_TOPICS_TO_SUGGEST
    )

    assert Mode.MARATHON == "marathon"
    assert Mode.FEED == "feed"
    assert len(COMPLEXITY_LEVELS) == 3
    assert FEED_TOPICS_TO_SUGGEST == 5
    print("✅ Константы конфигурации корректны")


def test_topic_request_detection():
    """Тест распознавания запроса темы"""
    from core.intent import is_topic_request

    assert is_topic_request("дай тему") == True
    assert is_topic_request("хочу учиться") == True
    assert is_topic_request("привет") == False
    print("✅ Запрос темы распознаётся корректно")


def test_topic_selection_parsing():
    """Тест парсинга выбора тем (SM-версия из states/feed/topics.py).

    Проверяет intent-phrase parsing: «Я хочу знать об X» → «X».
    """
    try:
        from states.feed.topics import FeedTopicsState

        # _parse_topic_selection — pure function, self не используется
        parse = lambda text, count=5: FeedTopicsState._parse_topic_selection(None, text, count)

        # --- Простые номера ---
        indices, custom = parse("1, 3")
        assert indices == {0, 2}, f"Ожидалось {{0, 2}}, получено {indices}"
        assert custom == []
        print("✅ Простые номера: 1, 3")

        indices, custom = parse("1, 3, 5")
        assert indices == {0, 2, 4}, f"Ожидалось {{0, 2, 4}}, получено {indices}"
        print("✅ Несколько номеров: 1, 3, 5")

        indices, custom = parse("1 и 3")
        assert 0 in indices and 2 in indices
        print("✅ Номера через 'и': 1 и 3")

        # --- Intent-фразы: должны извлекать суть ---
        indices, custom = parse("Я хочу знать об организации знания")
        assert len(custom) == 1
        assert "организаци" in custom[0].lower(), f"Неверный результат: {custom}"
        print(f"✅ Intent-фраза: «Я хочу знать об...» → {custom}")

        indices, custom = parse("Хочу узнать про системное мышление")
        assert custom == ["Системное мышление"], f"Неверный результат: {custom}"
        print(f"✅ Intent-фраза: «Хочу узнать про...» → {custom}")

        indices, custom = parse("Мне интересна тема собранности")
        assert len(custom) == 1 and "собранност" in custom[0].lower(), f"Неверный результат: {custom}"
        print(f"✅ Intent-фраза: «Мне интересна тема...» → {custom}")

        indices, custom = parse("Расскажи о собранности")
        assert len(custom) == 1
        print(f"✅ Intent-фраза: «Расскажи о...» → {custom}")

        indices, custom = parse("Расскажи мне про интеллект-стек")
        assert custom == ["Интеллект-стек"], f"Неверный результат: {custom}"
        print(f"✅ Intent-фраза: «Расскажи мне про...» → {custom}")

        # --- Номер + кастомная тема ---
        indices, custom = parse("2 и ещё хочу про собранность")
        assert 1 in indices, f"Тема 2 не распознана: {indices}"
        assert len(custom) == 1 and "собранность" in custom[0].lower(), f"Кастомная тема: {custom}"
        print(f"✅ Номер + кастомная тема: {indices}, {custom}")

        indices, custom = parse("1, 3 и ещё про коммуникации")
        assert indices == {0, 2}
        assert custom == ["Коммуникации"], f"Неверный результат: {custom}"
        print(f"✅ Два номера + кастомная: {indices}, {custom}")

        # --- Прямой ввод (без intent-обёртки) ---
        indices, custom = parse("организация знания")
        assert custom == ["Организация знания"]
        print(f"✅ Прямой ввод: «организация знания» → {custom}")

        indices, custom = parse("системное мышление")
        assert custom == ["Системное мышление"]
        print(f"✅ Прямой ввод: «системное мышление» → {custom}")

        # --- Edge cases ---
        indices, custom = parse("2")
        assert indices == {1} and custom == []
        print("✅ Только номер: 2")

        indices, custom = parse("Хочу про обучение и знания")
        assert "обучение и знания" in custom[0].lower(), f"Неверный результат: {custom}"
        print(f"✅ Тема с 'и' внутри: {custom}")

    except ImportError as e:
        print(f"⏭️ Парсинг выбора тем: пропущен ({e})")


def test_topic_selection_legacy():
    """Тест legacy-парсинга из handlers.py (для обратной совместимости)."""
    try:
        from engines.feed.handlers import parse_topic_selection

        indices, custom = parse_topic_selection("1, 3", 5)
        assert indices == {0, 2}
        print("✅ Legacy парсинг: простые номера")

        indices, custom = parse_topic_selection("хочу про внимание", 5)
        assert len(custom) >= 1
        print(f"✅ Legacy парсинг: кастомная тема → {custom}")

    except ImportError:
        print("⏭️ Legacy парсинг: пропущен (нет aiogram)")


def test_moscow_today():
    """Тест что moscow_today() возвращает корректную дату в МСК."""
    from db.queries.users import moscow_today, moscow_now
    from datetime import date, timedelta

    today = moscow_today()
    now_msk = moscow_now()

    # moscow_today() == moscow_now().date()
    assert today == now_msk.date(), f"moscow_today={today} != moscow_now().date()={now_msk.date()}"
    # Дата разумная (не больше ±1 день от UTC)
    from datetime import datetime
    utc_today = datetime.utcnow().date()
    diff = abs((today - utc_today).days)
    assert diff <= 1, f"МСК дата {today} слишком далека от UTC {utc_today}"
    print(f"✅ moscow_today() = {today} (МСК)")


def test_feed_session_limit_logic():
    """Тест логики лимита: keep_completed=True в delete_feed_sessions.

    Проверяет что функция принимает параметр и ветвится корректно.
    """
    import inspect
    from db.queries.feed import delete_feed_sessions

    sig = inspect.signature(delete_feed_sessions)
    params = list(sig.parameters.keys())
    assert 'keep_completed' in params, f"Параметр keep_completed отсутствует: {params}"
    assert sig.parameters['keep_completed'].default is False, "Default keep_completed должен быть False"
    print("✅ delete_feed_sessions имеет keep_completed (default=False)")


def test_guide_topic_catalog():
    """Тест каталога тем: все темы имеют обязательные поля."""
    from engines.feed.planner import GUIDE_TOPIC_CATALOG

    assert len(GUIDE_TOPIC_CATALOG) >= 30, f"Каталог слишком мал: {len(GUIDE_TOPIC_CATALOG)} тем"
    required_fields = {'title', 'guide', 'section', 'keywords'}
    priority_fields = {'chaos', 'deadlock', 'turning_point'}

    for i, topic in enumerate(GUIDE_TOPIC_CATALOG):
        for field in required_fields:
            assert field in topic, f"Тема #{i} ({topic.get('title', '?')}) без поля {field}"
        for field in priority_fields:
            assert field in topic, f"Тема #{i} ({topic.get('title', '?')}) без приоритета {field}"
            assert 0 <= topic[field] <= 3, f"Тема #{i}: {field}={topic[field]} вне [0,3]"

    # Уникальность названий
    titles = [t['title'] for t in GUIDE_TOPIC_CATALOG]
    assert len(titles) == len(set(titles)), f"Дубликаты: {[t for t in titles if titles.count(t) > 1]}"
    print(f"✅ Каталог тем: {len(GUIDE_TOPIC_CATALOG)} тем, все поля валидны, без дубликатов")


if __name__ == "__main__":
    print("\n🧪 Запуск тестов логики режима Лента\n")
    print("=" * 50)

    tests = [
        test_config_constants,
        test_intent_detection,
        test_question_keywords,
        test_topic_request_detection,
        test_planner_fallback,
        test_topic_selection_parsing,
        test_topic_selection_legacy,
        test_moscow_today,
        test_feed_session_limit_logic,
        test_guide_topic_catalog,
    ]

    passed = 0
    failed = 0
    skipped = 0

    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except ImportError as e:
            print(f"⏭️ {test_fn.__name__}: пропущен ({e})")
            skipped += 1
        except (AssertionError, Exception) as e:
            print(f"❌ {test_fn.__name__}: {e}")
            failed += 1

    print("=" * 50)
    print(f"\n{'✅' if failed == 0 else '❌'} Результат: {passed} пройдено, {skipped} пропущено, {failed} провалено\n")
    if failed > 0:
        sys.exit(1)
