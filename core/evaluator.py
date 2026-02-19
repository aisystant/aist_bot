"""
Оценщик ответов учеников (DS-evaluator-agent runtime).

Три функции:
- evaluate_answer() — проверка ответа по Bloom-уровню (3 оси: понятность, персонализация, тон)
- generate_fixation_note() — запись фиксации в fleeting-notes (для GitHub-пользователей)
- parse_evaluation() — парсинг FEEDBACK/SCORE/FIXATION из ответа Claude

Промпты и рубрики: DS-evaluator-agent/prompts/ и config/rubrics.yaml
Модель: CLAUDE_MODEL_HAIKU (latency <2 сек)
"""

import asyncio
from typing import Optional

from config import get_logger, CLAUDE_MODEL_HAIKU

logger = get_logger(__name__)


# ─── Рубрики (из DS-evaluator-agent/config/rubrics.yaml, inline для runtime) ───

TONE_ROLES = {
    1: "доброжелательный наставник, поддерживающий первые шаги",
    2: "внимательный преподаватель, помогающий выстраивать связи",
    3: "коллега-эксперт, обсуждающий на равных",
}

RUBRIC_CLARITY = {
    1: "Верно/неверно + правильный ответ простыми словами. Если ученик путает два понятия — покажи разницу.",
    2: "Суть уловлена или нет. Уточни связи между понятиями. Если есть пробел — укажи конкретно какой.",
    3: "Достаточна ли глубина анализа? Указаны ли пропущенные аспекты? Есть ли логические ошибки?",
}

RUBRIC_PERSONALIZATION = {
    1: "Обращайся по имени. Не ссылайся на профиль.",
    2: "Свяжи обратную связь с интересами ученика. Покажи, как тема относится к его сфере.",
    3: "Привяжи к целям ученика. Покажи, как эта тема помогает достичь цели.",
}

RUBRIC_TONE = {
    1: "Максимально поддерживающий. Поощряй любую попытку. Используй позитивные маркеры.",
    2: "Конструктивный. Баланс похвалы и развивающей обратной связи. Не упрощай.",
    3: "Профессиональный, коллегиальный. Без упрощений и снисходительности. Уважительная критика.",
}

BLOOM_EMOJI = {1: "🔵", 2: "🟡", 3: "🔴"}
BLOOM_NAMES = {1: "Различения", 2: "Понимание", 3: "Применение"}


def _build_evaluation_prompt(
    topic_title: str,
    bloom_level: int,
    intern: dict,
) -> tuple[str, str]:
    """Собирает system + user prompt для оценки ответа."""
    bl = max(1, min(bloom_level, 3))

    name = intern.get('name', '') or intern.get('occupation', '') or 'ученик'
    interests = intern.get('interests', '') or ''
    goals = intern.get('goals', '') or ''
    occupation = intern.get('occupation', '') or ''

    system_prompt = f"""Ты — {TONE_ROLES[bl]}.

ЗАДАЧА: Оцени ответ ученика на вопрос по теме «{topic_title}».

УРОВЕНЬ СЛОЖНОСТИ: {bl} — {BLOOM_NAMES[bl]}

РУБРИКА ОБРАТНОЙ СВЯЗИ:
- Понятность: {RUBRIC_CLARITY[bl]}
- Персонализация: {RUBRIC_PERSONALIZATION[bl]}
- Тон: {RUBRIC_TONE[bl]}

ПРОФИЛЬ УЧЕНИКА:
- Занятие: {occupation}
- Интересы: {interests}
- Цели: {goals}

ПРАВИЛА:
1. Ответ — 2-4 предложения (НЕ длинный разбор).
2. Начни с оценки (верно/частично/неверно), затем обратная связь.
3. Если ответ неточный — используй будущее время: «Попробуй в будущем ответить: ...», «В следующий раз обрати внимание на...». НЕ исправляй в настоящем времени.
4. НЕ повторяй вопрос. НЕ используй слово "отлично" больше одного раза.
5. Формат: Telegram Markdown v1 (*жирный*, НЕ **двойной**).

ФОРМАТ ОТВЕТА (строго):
FEEDBACK: <текст обратной связи, 2-4 предложения>
SCORE: <число 0.0-1.0>
FIXATION: <1 предложение — что запомнить из этой темы>"""

    return system_prompt, ""  # user_prompt заполняется в evaluate_answer


def parse_evaluation(raw: str) -> dict:
    """Парсит FEEDBACK/SCORE/FIXATION из ответа Claude.

    Returns:
        {"feedback": str, "score": float, "fixation": str}
    """
    result = {"feedback": "", "score": 0.5, "fixation": ""}

    for line in raw.split('\n'):
        line = line.strip()
        if line.startswith('FEEDBACK:'):
            result["feedback"] = line[9:].strip()
        elif line.startswith('SCORE:'):
            try:
                result["score"] = float(line[6:].strip())
            except ValueError:
                result["score"] = 0.5
        elif line.startswith('FIXATION:'):
            result["fixation"] = line[9:].strip()

    # Fallback: если формат не распознан — весь текст как feedback
    if not result["feedback"] and raw.strip():
        result["feedback"] = raw.strip()[:500]

    return result


async def evaluate_answer(
    answer_text: str,
    topic: dict,
    bloom_level: int,
    intern: dict,
    question_text: str = "",
) -> Optional[dict]:
    """Оценивает ответ ученика через Claude Haiku.

    Args:
        answer_text: ответ ученика
        topic: тема урока (dict с title, main_concept и т.д.)
        bloom_level: уровень Bloom (1-3)
        intern: профиль ученика
        question_text: текст вопроса (опционально, для контекста)

    Returns:
        {"feedback": str, "score": float, "fixation": str} или None при ошибке
    """
    from clients.claude import claude
    from core.knowledge import get_topic_title

    lang = intern.get('language', 'ru')
    topic_title = get_topic_title(topic, lang)

    system_prompt, _ = _build_evaluation_prompt(topic_title, bloom_level, intern)

    context = f"Вопрос: {question_text}\n\n" if question_text else ""
    user_prompt = f"""{context}Ответ ученика: {answer_text}

Оцени этот ответ. Выдай FEEDBACK, SCORE, FIXATION."""

    try:
        raw = await claude.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=500,
            model=CLAUDE_MODEL_HAIKU,
        )
        if not raw:
            logger.warning("Evaluator: Claude returned empty response")
            return None

        return parse_evaluation(raw)

    except Exception as e:
        logger.error(f"Evaluator error: {e}")
        return None


async def write_fixation_note(
    telegram_user_id: int,
    topic_title: str,
    bloom_level: int,
    fixation_text: str,
) -> None:
    """Записывает фиксацию в fleeting-notes (fire-and-forget).

    Условие: пользователь имеет GitHub-интеграцию.
    При ошибке — логируем, не показываем пользователю.
    """
    from clients.github_api import github_notes
    from clients.github_oauth import github_oauth

    try:
        access_token = await github_oauth.get_access_token(telegram_user_id)
        if not access_token:
            return  # нет GitHub-интеграции — тихо пропускаем

        bl = max(1, min(bloom_level, 3))
        emoji = BLOOM_EMOJI[bl]
        bloom_name = BLOOM_NAMES[bl]

        note_text = (
            f"Фиксация: {topic_title}\n"
            f"{emoji} Уровень: {bloom_name}\n"
            f"Ключевое: {fixation_text}"
        )

        await github_notes.append_note(
            telegram_user_id=telegram_user_id,
            text=note_text,
        )
        logger.info(f"Fixation written for user {telegram_user_id}: {topic_title}")

    except Exception as e:
        logger.warning(f"Fixation write failed for user {telegram_user_id}: {e}")


async def evaluate_and_fixate(
    answer_text: str,
    topic: dict,
    bloom_level: int,
    intern: dict,
    telegram_user_id: int,
    question_text: str = "",
) -> Optional[dict]:
    """Оценивает ответ + записывает фиксацию (fire-and-forget).

    Комбинированный метод для удобства вызова из стейтов.

    Returns:
        {"feedback": str, "score": float, "fixation": str} или None
    """
    from core.knowledge import get_topic_title

    result = await evaluate_answer(
        answer_text=answer_text,
        topic=topic,
        bloom_level=bloom_level,
        intern=intern,
        question_text=question_text,
    )

    if result and result.get("fixation"):
        lang = intern.get('language', 'ru')
        topic_title = get_topic_title(topic, lang)
        # Fire-and-forget: не блокируем основной flow
        asyncio.create_task(
            write_fixation_note(
                telegram_user_id=telegram_user_id,
                topic_title=topic_title,
                bloom_level=bloom_level,
                fixation_text=result["fixation"],
            )
        )

    return result
