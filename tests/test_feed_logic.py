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
    """Тест парсинга выбора тем"""
    try:
        from engines.feed.handlers import parse_topic_selection

        # Простые номера
        indices, custom = parse_topic_selection("1, 3", 5)
        assert indices == {0, 2}, f"Ожидалось {{0, 2}}, получено {indices}"
        assert custom == []
        print("✅ Простые номера: 1, 3 → корректно")

        # Номер и кастомная тема
        indices, custom = parse_topic_selection("2 и ещё хочу про собранность", 5)
        assert 1 in indices, f"Тема 2 не распознана: {indices}"
        assert "Собранность" in custom, f"Кастомная тема не распознана: {custom}"
        print("✅ Номер + кастомная тема → корректно")

        # Только кастомная тема
        indices, custom = parse_topic_selection("хочу про внимание", 5)
        assert "Внимание" in custom, f"Кастомная тема не распознана: {custom}"
        print("✅ Только кастомная тема → корректно")

        # Несколько тем
        indices, custom = parse_topic_selection("1, 3, 5", 5)
        assert indices == {0, 2, 4}, f"Ожидалось {{0, 2, 4}}, получено {indices}"
        print("✅ Несколько номеров: 1, 3, 5 → корректно")

    except ImportError:
        print("⏭️ Парсинг выбора тем: пропущен (нет aiogram)")


if __name__ == "__main__":
    print("\n🧪 Запуск тестов логики режима Лента\n")
    print("=" * 50)

    try:
        test_config_constants()
        test_intent_detection()
        test_question_keywords()
        test_topic_request_detection()
        test_planner_fallback()
        test_topic_selection_parsing()

        print("=" * 50)
        print("\n✅ Все тесты пройдены!\n")
    except AssertionError as e:
        print(f"\n❌ Тест провален: {e}\n")
        sys.exit(1)
    except ImportError as e:
        print(f"\n⚠️ Ошибка импорта: {e}")
        print("Убедитесь, что зависимости установлены: pip install pyyaml")
        sys.exit(1)
