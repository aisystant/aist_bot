"""
Распознавание интентов (намерений пользователя).

Определяет:
- question: пользователь задаёт вопрос
- answer: пользователь отвечает на задание
- topic_request: пользователь просит тему
- command: встроенная команда (проще, глубже, примеры)
- unknown: не удалось распознать
"""

import re
from typing import Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from config import (
    get_logger,
    QUESTION_WORDS,
    TOPIC_REQUEST_PATTERNS,
    COMMAND_WORDS,
)

logger = get_logger(__name__)


class IntentType(Enum):
    """Типы интентов"""
    QUESTION = "question"           # Вопрос пользователя (требует ответа через MCP)
    ANSWER = "answer"               # Ответ на задание
    TOPIC_REQUEST = "topic_request" # Запрос следующей темы
    COMMAND = "command"             # Встроенная команда
    FEEDBACK = "feedback"           # Обратная связь / комментарий
    UNKNOWN = "unknown"             # Не распознано


@dataclass
class Intent:
    """Результат распознавания интента"""
    type: IntentType
    confidence: float = 1.0
    command: Optional[str] = None  # Для IntentType.COMMAND
    original_text: str = ""


def detect_intent(text: str, context: dict = None) -> Intent:
    """Определяет интент пользователя на основе текста сообщения

    Args:
        text: текст сообщения пользователя
        context: контекст (текущий режим, состояние FSM и т.д.)
            - awaiting_answer: bool - ждём ответ на вопрос
            - awaiting_work_product: bool - ждём рабочий продукт
            - mode: str - текущий режим (marathon/feed)

    Returns:
        Intent с типом и дополнительной информацией
    """
    context = context or {}
    text_lower = text.lower().strip()
    original = text

    # 1. Проверяем встроенные команды
    command = detect_command(text_lower)
    if command:
        return Intent(
            type=IntentType.COMMAND,
            command=command,
            original_text=original
        )

    # 2. Если ждём ответ на вопрос или рабочий продукт - это ответ
    if context.get('awaiting_answer') or context.get('awaiting_work_product'):
        # Но проверим, не задаёт ли пользователь вопрос вместо ответа
        if is_clear_question(text_lower):
            return Intent(
                type=IntentType.QUESTION,
                confidence=0.9,
                original_text=original
            )
        return Intent(
            type=IntentType.ANSWER,
            original_text=original
        )

    # 3. Проверяем запрос темы
    if is_topic_request(text_lower):
        return Intent(
            type=IntentType.TOPIC_REQUEST,
            confidence=0.9,
            original_text=original
        )

    # 4. Проверяем вопрос
    question_score = question_likelihood(text_lower)
    if question_score >= 0.7:
        return Intent(
            type=IntentType.QUESTION,
            confidence=question_score,
            original_text=original
        )

    # 5. Короткие сообщения без контекста - возможно feedback
    if len(text) < 20 and not context.get('mode'):
        return Intent(
            type=IntentType.FEEDBACK,
            confidence=0.6,
            original_text=original
        )

    # 6. Если есть контекст и текст содержательный - вероятно ответ
    if context.get('mode') and len(text) > 50:
        return Intent(
            type=IntentType.ANSWER,
            confidence=0.7,
            original_text=original
        )

    # Не удалось распознать
    return Intent(
        type=IntentType.UNKNOWN,
        confidence=0.5,
        original_text=original
    )


def detect_command(text: str) -> Optional[str]:
    """Проверяет, является ли текст встроенной командой

    Args:
        text: текст в нижнем регистре

    Returns:
        Название команды или None
    """
    text = text.strip()

    # Точное совпадение
    if text in COMMAND_WORDS:
        return COMMAND_WORDS[text]

    # Проверяем начало строки
    for word, cmd in COMMAND_WORDS.items():
        if text.startswith(word):
            return cmd

    return None


def is_topic_request(text: str) -> bool:
    """Проверяет, запрашивает ли пользователь тему

    Args:
        text: текст в нижнем регистре

    Returns:
        True если это запрос темы
    """
    # Явные паттерны
    patterns = [
        r'дай\s+тему',
        r'давай\s+тему',
        r'следующ\w+\s+тем',
        r'хочу\s+учиться',
        r'хочу\s+изучать',
        r'предложи\s+тем',
        r'начать\s+марафон',
        r'начать\s+ленту',
        r'продолжить\s+марафон',
        r'продолжить\s+ленту',
    ]

    for pattern in patterns:
        if re.search(pattern, text):
            return True

    # Проверяем TOPIC_REQUEST_PATTERNS как слова
    words = set(text.split())
    for pattern_word in TOPIC_REQUEST_PATTERNS:
        if pattern_word in words:
            # Проверяем что это не просто "хочу спросить"
            if 'тем' in text or 'учи' in text or 'изуч' in text:
                return True

    return False


def is_clear_question(text: str) -> bool:
    """Проверяет, является ли текст явным вопросом

    Args:
        text: текст в нижнем регистре

    Returns:
        True если это явный вопрос
    """
    # Заканчивается на вопросительный знак
    if text.rstrip().endswith('?'):
        return True

    # Начинается с вопросительного слова
    first_word = text.split()[0] if text.split() else ''
    if first_word in QUESTION_WORDS:
        return True

    # Проверяем начало с вопросительных конструкций
    question_starters = [
        'можно ли', 'нельзя ли', 'не могли бы',
        'а что если', 'а как', 'а почему', 'а зачем',
        'скажи', 'расскажи', 'объясни', 'поясни',
        'в чём', 'в чем',
    ]

    for starter in question_starters:
        if text.startswith(starter):
            return True

    return False


def question_likelihood(text: str) -> float:
    """Вычисляет вероятность того, что текст является вопросом

    Args:
        text: текст в нижнем регистре

    Returns:
        Вероятность от 0 до 1
    """
    score = 0.0

    # Вопросительный знак
    if '?' in text:
        score += 0.5

    # Начинается с вопросительного слова
    words = text.split()
    if words and words[0] in QUESTION_WORDS:
        score += 0.3

    # Содержит вопросительные слова внутри
    for word in QUESTION_WORDS:
        if word in words:
            score += 0.1
            break  # Только один раз

    # Вопросительные конструкции
    question_phrases = [
        'можно ли', 'как это', 'что значит', 'что такое',
        'в чём разница', 'в чем разница', 'чем отличается',
        'почему так', 'зачем нужен', 'как работает',
    ]
    for phrase in question_phrases:
        if phrase in text:
            score += 0.3
            break

    # Длинный текст без вопросительных признаков - скорее не вопрос
    if len(text) > 200 and score < 0.3:
        score *= 0.5

    return min(score, 1.0)


def get_question_keywords(text: str) -> list:
    """Извлекает ключевые слова из вопроса для поиска в MCP

    Args:
        text: текст вопроса

    Returns:
        Список ключевых слов
    """
    # Удаляем вопросительные слова и стоп-слова
    stop_words = set(QUESTION_WORDS) | {
        # Указательные и определительные
        'это', 'то', 'такое', 'такой', 'такая', 'такие',
        'этот', 'эта', 'эти', 'тот', 'та', 'те',
        # Частицы
        'ли', 'же', 'бы', 'не', 'ни', 'да', 'нет', 'вот', 'ведь',
        # Местоимения
        'мне', 'вам', 'нам', 'им', 'ему', 'ей', 'её', 'его',
        'она', 'оно', 'они', 'меня', 'тебя', 'себя',
        'мой', 'моя', 'моё', 'мои', 'твой', 'свой',
        # Союзы и предлоги
        'и', 'а', 'но', 'или', 'если', 'так', 'что', 'как',
        'для', 'от', 'по', 'на', 'из', 'за', 'про', 'при',
        'чем', 'чём', 'кем', 'ком', 'чего', 'кого',
        # Глаголы-связки и вспомогательные
        'есть', 'быть', 'был', 'была', 'было', 'были',
        'будет', 'будут', 'можно', 'нужно', 'надо',
        # Наречия
        'очень', 'просто', 'только', 'ещё', 'еще', 'уже',
        'тоже', 'также', 'потом', 'сейчас', 'теперь',
        # Оценочные слова (не несут информации для поиска)
        'полезна', 'полезно', 'полезный', 'полезные',
        'хорошо', 'плохо', 'лучше', 'хуже',
    }

    # Очищаем текст
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text)
    words = text.split()

    # Фильтруем: убираем стоп-слова и слова короче 3 символов
    keywords = [w for w in words if w not in stop_words and len(w) > 2]

    return keywords[:5]  # Максимум 5 ключевых слов
