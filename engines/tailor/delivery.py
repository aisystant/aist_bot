"""
Доставка занятий Портного через scheduler (WP-149, SC.020).

Паттерн аналогичен pre_generate_feed_digest():
1. Собрать занятие (TailorEngine.assemble)
2. Сгенерировать текст (generate_lesson_text)
3. Доставить через BotTailorAdapter (с кнопками Ответить / Пропустить)

MVP: доставка по feed_schedule_time для всех с feed_status='active'.
"""

import asyncio
import json
import logging
from typing import Optional

from aiogram import Bot

logger = logging.getLogger(__name__)


async def deliver_tailor_lesson(chat_id: int, bot: Bot, force: bool = False):
    """Сгенерировать и отправить персональное занятие.

    Вызывается из scheduler по расписанию (feed_schedule_time).
    Паттерн: pre-gen → save → notify (аналогично feed digest).

    Args:
        force: пропустить idempotency guard (для /tailor dev-команды).
    """
    from db.queries import get_intern
    from db.queries.cache import cache_get, cache_set
    from db.queries.events import log_event
    from db.queries.notifications import try_insert_notification
    from db.queries.users import moscow_today

    from engines.tailor.bot_adapter import BotTailorAdapter

    intern = await get_intern(chat_id)
    if not intern:
        return

    # ─── Guard: onboarding пройден ───
    if not intern.get('onboarding_completed'):
        return

    # ─── Idempotency: не отправлять дважды в день ───
    today = moscow_today()
    today_str = today.strftime('%Y-%m-%d')
    if not force:
        tailor_key = f"tailor_lesson:{chat_id}:{today_str}"
        if not await try_insert_notification(chat_id, 'tailor_lesson', tailor_key):
            logger.info(f"[Tailor] Lesson already sent to {chat_id} today, skip")
            return

    # ─── Проверить кэш ───
    cache_key = f"tailor:{chat_id}:{today_str}"
    cached_raw = await cache_get(cache_key)
    lesson = None
    generated = None

    if cached_raw:
        try:
            cached = json.loads(cached_raw)
            if cached.get('lesson') and cached.get('generated'):
                logger.info(f"[Tailor] Using cached lesson for {chat_id}")
                lesson = cached['lesson']
                generated = cached['generated']
        except (json.JSONDecodeError, TypeError):
            pass

    if not lesson or not generated:
        # ─── Сборка + генерация ───
        result = await _assemble_and_generate(chat_id, intern)
        if not result:
            logger.warning(f"[Tailor] Failed to generate for {chat_id}")
            return

        lesson, generated = result

        # Кэшируем на сутки (lesson + generated для callback)
        await cache_set(
            cache_key,
            'tailor_lesson',
            json.dumps({'lesson': lesson, 'generated': generated}, ensure_ascii=False),
            ttl_days=1,
        )

    # ─── Доставка через BotTailorAdapter ───
    adapter = BotTailorAdapter(bot)
    delivery_result = await adapter.deliver(chat_id, lesson, generated)

    if delivery_result.delivered:
        # Log event (fire-and-forget)
        await log_event(
            user_id=chat_id,
            event_type='tailor_lesson_sent',
            payload={
                'date': today_str,
                'element_id': lesson.get('element_id', ''),
                'element_type': lesson.get('element_type', ''),
                'area': lesson.get('area', 1),
                'impact_type': lesson.get('impact_type', ''),
                'depth': lesson.get('depth', 1),
            },
            source='bot',
        )
    else:
        logger.error(
            f"[Tailor] Delivery failed for {chat_id}: "
            f"{delivery_result.error}"
        )


async def _assemble_and_generate(
    chat_id: int,
    intern: dict,
) -> Optional[tuple]:
    """Собрать занятие через TailorEngine + сгенерировать текст.

    Returns:
        (lesson, generated) tuple или None при ошибке.
    """
    from engines.tailor.engine import TailorEngine
    from engines.tailor.planner import generate_lesson_text

    # Построить профиль из intern (public.users + user_state)
    user_profile = {
        'name': intern.get('name', ''),
        'occupation': intern.get('occupation', ''),
        'goals': intern.get('goals', ''),
        'interests': intern.get('interests', ''),
        'assessment_state': intern.get('assessment_state', 'turn'),
        'student_stage': 0,  # MVP: все начинают с 0
        'it_level': 0,       # MVP: предполагаем ИТ=0
        'energy': 3,         # MVP: средняя энергия
        'learning_style': intern.get('learning_style', ''),
    }

    # Получить историю из learning_history (если есть)
    learning_history = await _get_learning_history(chat_id)

    # Определить вчерашнюю область
    last_area = None
    if learning_history:
        last_area = learning_history[-1].get('area')

    # ─── Шаги 1-7 SOP.001 v3 ───
    tailor = TailorEngine()
    lesson = tailor.assemble(
        user_profile=user_profile,
        learning_history=learning_history,
        last_area=last_area,
    )
    if not lesson:
        logger.warning(f"[Tailor] assemble returned None for {chat_id}")
        return None

    # ─── Генерация текста через Claude ───
    try:
        from bot import claude  # lazy import (circular dep)
        generated = await asyncio.wait_for(
            generate_lesson_text(
                lesson=lesson,
                user_profile=user_profile,
                claude_client=claude,
            ),
            timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error(f"[Tailor] Claude timeout for {chat_id}")
        return None

    if not generated:
        # Fallback: отправить raw ячейку без генерации
        generated = _fallback_from_cell(lesson)

    return lesson, generated


async def _get_learning_history(chat_id: int) -> list:
    """Получить историю прохождения из learning_history."""
    try:
        from db.connection import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT topic_id, bloom_level, score, passed,
                       direction, errors, created_at
                FROM development.learning_history
                WHERE user_id = $1
                ORDER BY created_at ASC
            ''', chat_id)
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[Tailor] learning_history read failed: {e}")
        return []


def _fallback_from_cell(lesson: dict) -> dict:
    """Fallback: structured content из ячейки без Claude."""
    can_do = lesson.get('can_do', [])
    can_do_text = "\n".join(f"• {cd}" for cd in can_do)

    assessment = lesson.get('assessment', {})
    form = lesson.get('form', '')

    return {
        'intro': f"Сегодня разберём тему: {lesson.get('topic_name', '')}",
        'lesson_text': (
            f"{lesson.get('methods_notes', '')}\n\n"
            f"После этого занятия ты сможешь:\n{can_do_text}"
        ),
        'question': assessment.get('transfer_test', ''),
        'task': form,
        'reflection': assessment.get('retrieval_test', ''),
    }
