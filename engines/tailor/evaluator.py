"""
Оценка ответа пользователя на занятие Портного (WP-149, SC.020).

Канало-независимый модуль: получает текст ответа + can-do критерии,
возвращает score и обратную связь. Не знает о Telegram, FSM, кнопках.
"""

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


async def evaluate_answer(
    answer_text: str,
    lesson: dict,
    claude_client,
    model: Optional[str] = None,
) -> dict:
    """Оценить ответ пользователя по can-do критериям.

    Args:
        answer_text: текст ответа пользователя
        lesson: structured lesson (из кэша или TailorEngine.assemble())
        claude_client: ClaudeClient
        model: модель Claude (default: Haiku — быстро, дёшево)

    Returns:
        {
            'score': float (0.0–1.0),
            'passed': bool,
            'feedback': str,
            'errors': list[str],
        }
    """
    try:
        from config import CLAUDE_MODEL_HAIKU
        model = model or CLAUDE_MODEL_HAIKU
    except ImportError:
        model = model or 'claude-haiku-4-5-20251001'

    can_do = lesson.get('can_do', [])
    topic_name = lesson.get('topic_name', '')
    bloom_level = lesson.get('bloom_level', 'Remember')
    question = lesson.get('assessment', {}).get('transfer_test', '')

    prompt = _build_eval_prompt(
        answer_text=answer_text,
        can_do=can_do,
        topic_name=topic_name,
        bloom_level=bloom_level,
        question=question,
    )

    try:
        response = await claude_client.generate(
            prompt=prompt,
            max_tokens=512,
            model=model,
        )
        if not response:
            return _default_result()
        return _parse_eval_response(response)
    except Exception as e:
        logger.warning(f"[Tailor/Eval] Claude evaluation failed: {e}")
        return _default_result()


def _build_eval_prompt(
    answer_text: str,
    can_do: list,
    topic_name: str,
    bloom_level: str,
    question: str,
) -> str:
    """Построить промпт для оценки."""
    can_do_text = "\n".join(f"  - {cd}" for cd in can_do)

    return f"""Ты — Оценщик (Evaluator) в системе персонального развития.
Оцени ответ ученика на вопрос по теме «{topic_name}» (уровень: {bloom_level}).

## Вопрос
{question}

## Ответ ученика
{answer_text}

## Критерии (can-do):
{can_do_text}

## Задача
Оцени ответ по критериям. Верни JSON:

```json
{{
  "score": 0.85,
  "passed": true,
  "feedback": "Краткая обратная связь (2-3 предложения, на русском, поддерживающий тон)",
  "errors": ["ошибка 1"]
}}
```

Правила:
- score: 0.0–1.0 (0.7+ = passed)
- passed: true если score >= 0.7
- feedback: конкретный, с привязкой к ответу. НЕ общие фразы
- errors: только реальные ошибки в ответе (пустой список если нет)
- Тон: наставник, не экзаменатор"""


def _parse_eval_response(response: str) -> dict:
    """Извлечь JSON из ответа Claude."""
    json_match = re.search(r'```json\s*\n?(.*?)\n?```', response, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group(1))
            return _validate_result(result)
        except json.JSONDecodeError:
            pass

    try:
        result = json.loads(response)
        return _validate_result(result)
    except json.JSONDecodeError:
        pass

    logger.warning("[Tailor/Eval] Could not parse evaluation JSON")
    return _default_result()


def _validate_result(result: dict) -> dict:
    """Валидация и нормализация результата."""
    score = float(result.get('score', 0.5))
    score = max(0.0, min(1.0, score))
    passed = result.get('passed', score >= 0.7)
    feedback = result.get('feedback', '')
    errors = result.get('errors', [])
    if isinstance(errors, str):
        errors = [errors] if errors else []
    return {
        'score': score,
        'passed': bool(passed),
        'feedback': str(feedback),
        'errors': list(errors),
    }


def _default_result() -> dict:
    """Дефолтный результат при ошибке оценки."""
    return {
        'score': 0.5,
        'passed': False,
        'feedback': 'Спасибо за ответ! Мы его записали.',
        'errors': [],
    }
