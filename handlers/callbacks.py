from __future__ import annotations

"""
Тонкие aiogram callback хендлеры.

Роутят callback queries в Dispatcher / State Machine.
"""

import asyncio
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from db.queries import get_intern
from i18n import t

logger = logging.getLogger(__name__)

callbacks_router = Router(name="callbacks")


# === Noop (разделители меню) ===

@callbacks_router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    """Разделитель в меню — без действия."""
    await callback.answer()


# === Сервисный реестр: единая точка входа для service:* callbacks ===

@callbacks_router.callback_query(F.data.startswith("service:"))
async def cb_service_select(callback: CallbackQuery, state: FSMContext):
    """Callback из главного меню (service registry).

    Формат callback_data: "service:{service_id}"
    Роутит в entry_state сервиса из реестра.
    """
    from handlers import get_dispatcher
    from core.registry import registry

    dispatcher = get_dispatcher()
    await callback.answer()

    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return

    service = registry.resolve_callback(callback.data)
    if not service:
        logger.warning(f"[CB] Unknown service callback: {callback.data}")
        return

    if not (dispatcher and dispatcher.is_sm_active):
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.processing_error', lang))
        return

    # Определяем entry_state с учётом режима (mode-aware)
    entry_state = service.get_entry_state(intern)
    logger.info(f"[CB] Service select: {service.id} → {entry_state}")

    # Записываем аналитику использования сервиса
    await registry.record_usage(callback.message.chat.id, service.id)

    await state.clear()
    await callback.message.edit_reply_markup()
    await dispatcher.go_to(intern, entry_state)


# === Legacy callbacks (обратная совместимость) ===

@callbacks_router.callback_query(F.data == "learn")
async def cb_learn(callback: CallbackQuery, state: FSMContext):
    """Callback 'Учиться' — mode-aware через Dispatcher."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    await callback.answer()
    await callback.message.edit_reply_markup()

    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return

    if dispatcher and dispatcher.is_sm_active:
        await state.clear()
        await dispatcher.route_learn(intern)
        return

    lang = intern.get('language', 'ru') or 'ru'
    await callback.message.answer(t('errors.processing_error', lang))


@callbacks_router.callback_query(F.data == "later")
async def cb_later(callback: CallbackQuery):
    """Callback 'Позже'."""
    intern = await get_intern(callback.message.chat.id)
    lang = intern.get('language', 'ru') or 'ru'
    await callback.answer()
    await callback.message.edit_text(t('fsm.see_you_later', lang, time=intern['schedule_time']))


@callbacks_router.callback_query(F.data == "feed")
async def cb_feed(callback: CallbackQuery, state: FSMContext):
    """Callback для входа в Ленту."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    await callback.answer()
    await callback.message.edit_reply_markup()

    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return

    if dispatcher and dispatcher.is_sm_active:
        await state.clear()
        await dispatcher.route_command('feed', intern)
        return

    lang = intern.get('language', 'ru') or 'ru'
    await callback.message.answer(t('feed.not_available', lang))


# === Tailor (Портной, WP-149, SC.020) ===

@callbacks_router.callback_query(F.data.startswith("tailor_"))
async def cb_tailor_actions(callback: CallbackQuery, state: FSMContext):
    """Обработка Tailor callback-ов (Ответить / Пропустить)."""
    from handlers import get_dispatcher
    from engines.tailor.bot_adapter import CB_TAILOR_ANSWER, CB_TAILOR_SKIP

    dispatcher = get_dispatcher()
    chat_id = callback.message.chat.id
    data = callback.data
    intern = await get_intern(chat_id)

    if not intern:
        await callback.answer()
        return

    await callback.answer()
    lang = intern.get('language', 'ru') or 'ru'

    if not (dispatcher and dispatcher.is_sm_active):
        await callback.message.answer(t('errors.processing_error', lang))
        return

    # Парсим callback_data: tailor_answer:SS.F1.01:1:2 или tailor_skip:SS.F1.01:1:2
    parts = data.split(":")
    if len(parts) < 4:
        logger.warning(f"[CB] Invalid tailor callback: {data}")
        return

    action = parts[0]  # tailor_answer или tailor_skip
    topic_id = parts[1]
    bloom_depth = int(parts[2])
    direction = int(parts[3])

    # Загрузить закэшированное занятие для передачи в стейт
    lesson = await _get_cached_tailor_lesson(chat_id)

    if action == CB_TAILOR_SKIP:
        # Пропуск: записать score=0 без перехода в стейт
        try:
            from db.queries.events import log_event
            await log_event(
                user_id=chat_id,
                event_type='learning_completed',
                payload={
                    'program_id': 'SS.F1',
                    'topic_id': topic_id,
                    'direction': direction,
                    'bloom_level': bloom_depth,
                    'cell_id': f"{topic_id}@{bloom_depth}",
                    'score': 0.0,
                    'passed': False,
                    'errors': [],
                    '_schema_version': 1,
                },
                source='bot',
            )
        except Exception as e:
            logger.warning(f"[CB] Tailor skip log failed: {e}")

        await callback.message.edit_reply_markup()
        await callback.message.answer(
            "⏭ Занятие пропущено. Тема вернётся в следующем цикле."
        )
        return

    # tailor_answer → перейти в tailor.response
    await state.clear()
    await callback.message.edit_reply_markup()
    await dispatcher.go_to(intern, "tailor.response", context={
        'topic_id': topic_id,
        'bloom_depth': bloom_depth,
        'direction': direction,
        'lesson': lesson,
    })


async def _get_cached_tailor_lesson(chat_id: int) -> dict:
    """Загрузить structured lesson из кэша для контекста стейта."""
    try:
        import json as _json
        from db.queries.cache import cache_get
        from db.queries.users import moscow_today
        today_str = moscow_today().strftime('%Y-%m-%d')
        cache_key = f"tailor:{chat_id}:{today_str}"
        cached_raw = await cache_get(cache_key)
        if cached_raw:
            cached = _json.loads(cached_raw)
            return cached.get('lesson', {})
    except Exception as e:
        logger.warning(f"[CB] Tailor lesson cache read failed: {e}")
    return {}


# === Marathon ===

@callbacks_router.callback_query(F.data.startswith("marathon_"))
async def cb_marathon_actions(callback: CallbackQuery, state: FSMContext):
    """Обработка Marathon callback-ов (Получить урок/вопрос/практику)."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    chat_id = callback.message.chat.id
    data = callback.data
    intern = await get_intern(chat_id)

    if not intern or not (dispatcher and dispatcher.is_sm_active):
        await callback.answer()
        return

    logger.info(f"[CB] Marathon callback '{data}' for chat_id={chat_id}")

    try:
        # Direct entry callbacks — route via go_to (from menu / mode_select)
        if data in ("marathon_get_lesson", "marathon_get_question", "marathon_get_practice",
                     "marathon_catchup_today"):
            await callback.answer()
            try:
                await callback.message.edit_reply_markup()
            except Exception:
                pass
            await state.clear()

            # WP-151 Ф3: reminder_opened
            if data == "marathon_get_lesson":
                from db.queries.events import log_event
                await log_event(chat_id, 'reminder_opened', {
                    'source': 'marathon_get_lesson',
                })

            if data == "marathon_catchup_today":
                # Catch-up: user wants today's lesson after completing yesterday's
                lang = intern.get('language', 'ru') or 'ru'
                await callback.message.answer(
                    f"⏳ {t('reminders.marathon_catchup_generating', lang)}"
                )
                await dispatcher.go_to(intern, "workshop.marathon.lesson")
            else:
                state_map = {
                    "marathon_get_lesson": "workshop.marathon.lesson",
                    "marathon_get_question": "workshop.marathon.question",
                    "marathon_get_practice": "workshop.marathon.task",
                }
                await dispatcher.go_to(intern, state_map[data])

        elif data == "marathon_catchup_no":
            # User declines catch-up
            await callback.answer()
            try:
                await callback.message.edit_reply_markup()
            except Exception:
                pass
            lang = intern.get('language', 'ru') or 'ru'
            await callback.message.answer(
                f"_{t('marathon.come_back_tomorrow', lang)}_",
                parse_mode="Markdown"
            )
        else:
            # In-state callbacks (next_question, next_bonus, retry, back, etc.)
            # Route to SM — state's handle_callback will answer() and process
            await dispatcher.route_callback(intern, callback)

    except Exception as e:
        logger.error(f"[CB] Error handling marathon callback: {e}")
        import traceback
        logger.error(traceback.format_exc())
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.try_again', lang))


@callbacks_router.callback_query(F.data.startswith("feed_"))
async def cb_feed_actions(callback: CallbackQuery, state: FSMContext):
    """Обработка всех Feed-специфичных callback-ов через SM."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    intern = await get_intern(callback.message.chat.id)
    if not intern:
        await callback.answer()
        return

    if not (dispatcher and dispatcher.is_sm_active):
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('feed.not_available', lang))
        return

    data = callback.data
    logger.info(f"[CB] Feed callback '{data}' for chat_id={callback.message.chat.id}")

    try:
        current_state = intern.get('current_state', '')

        if data == "feed_get_digest":
            await callback.answer()
            await callback.message.edit_reply_markup()
            await state.clear()
            await dispatcher.go_to(intern, "feed.digest")

        elif data == "feed_topics_menu":
            await callback.answer()
            await callback.message.edit_reply_markup()
            await state.clear()
            await dispatcher.go_to(intern, "feed.digest", context={"show_topics_menu": True})

        elif data == "feed_reset_topics":
            # Перегенерация тем: передаём force_regenerate в feed.topics
            # enter() сам сбросит ACTIVE неделю и сгенерирует новые темы
            await callback.answer()
            try:
                await callback.message.edit_reply_markup()
            except Exception:
                pass
            await state.clear()
            await dispatcher.go_to(intern, "feed.topics", context={"force_regenerate": True})

        elif current_state.startswith("feed."):
            # Пользователь уже в Feed-стейте — передаём callback в SM
            await dispatcher.route_callback(intern, callback)

        else:
            logger.warning(f"[CB] User in state '{current_state}' clicked '{data}', routing to feed.digest")
            await callback.answer()
            await callback.message.edit_reply_markup()
            await state.clear()
            await dispatcher.go_to(intern, "feed.digest")

    except Exception as e:
        logger.error(f"[CB] Error handling feed callback: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.try_again', lang))


async def _is_in_sm_mode_select_state(callback: CallbackQuery) -> bool | dict:
    """Фильтр: пользователь в common.mode_select стейте SM. Returns dict to avoid double get_intern."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    if not (dispatcher and dispatcher.is_sm_active):
        return False
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return False
    if intern.get('current_state') != "common.mode_select":
        return False
    return {"intern": intern}


@callbacks_router.callback_query(
    F.data.in_({"show_language", "lang_back"}) | F.data.startswith("lang_"),
    _is_in_sm_mode_select_state
)
async def cb_mode_select_language(callback: CallbackQuery, state: FSMContext, intern: dict):
    """Language callback из главного меню через SM."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    logger.info(f"[CB] Mode select language callback '{callback.data}' for chat_id={callback.message.chat.id}")
    try:
        await dispatcher.route_callback(intern, callback)
    except Exception as e:
        logger.error(f"[CB] Error handling mode_select language callback: {e}")
        await callback.answer()


async def _is_in_sm_profile_or_settings_state(callback: CallbackQuery) -> bool | dict:
    """Фильтр: пользователь в common.profile или common.settings стейте SM. Returns dict to avoid double get_intern."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    if not (dispatcher and dispatcher.is_sm_active):
        return False
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return False
    current = intern.get('current_state', '')
    if current not in ("common.profile", "common.settings"):
        return False
    return {"intern": intern}


@callbacks_router.callback_query(
    F.data.startswith("upd_") | F.data.startswith("settings_") | F.data.startswith("duration_") | F.data.startswith("bloom_") | F.data.startswith("lang_") | F.data.startswith("conn_") | F.data.startswith("github_") | F.data.startswith("reset_") | (F.data == "show_resets") | (F.data == "show_commands"),
    _is_in_sm_profile_or_settings_state
)
async def cb_settings_actions(callback: CallbackQuery, state: FSMContext, intern: dict):
    """Profile/Settings callback-ы через SM."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    logger.info(f"[CB] Profile/Settings callback '{callback.data}' for chat_id={callback.message.chat.id}")
    try:
        await dispatcher.route_callback(intern, callback)
    except Exception as e:
        logger.error(f"[CB] Error handling profile/settings callback: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.try_again', lang))


@callbacks_router.callback_query(F.data == "go_update")
async def cb_go_update(callback: CallbackQuery, state: FSMContext):
    """Переход к настройкам из progress и других мест."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    await callback.answer()
    intern = await get_intern(callback.message.chat.id)

    if dispatcher and dispatcher.is_sm_active and intern:
        try:
            await state.clear()
            await callback.message.delete()
            await dispatcher.route_command('update', intern)
            return
        except Exception as e:
            logger.error(f"[CB] Error routing go_update: {e}")

    # Legacy fallback
    if not intern:
        return
    from handlers.settings import _show_update_screen
    await _show_update_screen(callback.message, intern, state)


@callbacks_router.callback_query(F.data == "go_profile")
async def cb_go_profile(callback: CallbackQuery, state: FSMContext):
    """Переход к профилю из progress и других мест."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    await callback.answer()
    intern = await get_intern(callback.message.chat.id)

    if dispatcher and dispatcher.is_sm_active and intern:
        try:
            await state.clear()
            await callback.message.delete()
            await dispatcher.route_command('profile', intern)
            return
        except Exception as e:
            logger.error(f"[CB] Error routing go_profile: {e}")

    # Legacy fallback — просто отправляем /profile
    if intern:
        await callback.message.delete()
        await callback.message.answer("/profile — используйте для перехода в профиль")


@callbacks_router.callback_query(F.data == "go_mydata")
async def cb_go_mydata(callback: CallbackQuery, state: FSMContext):
    """Переход к «Мои данные» из progress и других мест."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    await callback.answer()
    intern = await get_intern(callback.message.chat.id)

    if dispatcher and dispatcher.is_sm_active and intern:
        try:
            await state.clear()
            await callback.message.delete()
            await dispatcher.route_command('mydata', intern)
            return
        except Exception as e:
            logger.error(f"[CB] Error routing go_mydata: {e}")

    if intern:
        await callback.message.delete()
        await callback.message.answer("/mydata — используйте для просмотра данных")


@callbacks_router.callback_query(F.data == "go_progress")
async def cb_go_progress(callback: CallbackQuery, state: FSMContext):
    """Переход к прогрессу."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    await callback.answer()
    intern = await get_intern(callback.message.chat.id)

    if dispatcher and dispatcher.is_sm_active and intern:
        try:
            await state.clear()
            await callback.message.delete()
            await dispatcher.route_command('progress', intern)
            return
        except Exception as e:
            logger.error(f"[CB] Error routing go_progress: {e}")

    from handlers.progress import cmd_progress
    await cmd_progress(callback.message)


async def _is_in_sm_progress_state(callback: CallbackQuery) -> bool | dict:
    """Фильтр: пользователь в utility.progress стейте SM. Returns dict to avoid double get_intern."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    if not (dispatcher and dispatcher.is_sm_active):
        return False
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return False
    if intern.get('current_state') != "utility.progress":
        return False
    return {"intern": intern}


@callbacks_router.callback_query(
    F.data.startswith("progress_"),
    _is_in_sm_progress_state
)
async def cb_progress_actions(callback: CallbackQuery, state: FSMContext, intern: dict):
    """Progress section callback-ы через SM."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    logger.info(f"[CB] Progress callback '{callback.data}' for chat_id={callback.message.chat.id}")
    try:
        await dispatcher.route_callback(intern, callback)
    except Exception as e:
        logger.error(f"[CB] Error handling progress callback: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.try_again', lang))


async def _is_in_sm_plans_state(callback: CallbackQuery) -> bool | dict:
    """Фильтр: пользователь в common.plans стейте SM. Returns dict to avoid double get_intern."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    if not (dispatcher and dispatcher.is_sm_active):
        return False
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return False
    if intern.get('current_state') != "common.plans":
        return False
    return {"intern": intern}


@callbacks_router.callback_query(
    F.data.startswith("plans_"),
    _is_in_sm_plans_state
)
async def cb_plans_actions(callback: CallbackQuery, state: FSMContext, intern: dict):
    """Plans callback-ы через SM."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    logger.info(f"[CB] Plans callback '{callback.data}' for chat_id={callback.message.chat.id}")
    try:
        await dispatcher.route_callback(intern, callback)
    except Exception as e:
        logger.error(f"[CB] Error handling plans callback: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.try_again', lang))


async def _is_in_sm_assessment_state(callback: CallbackQuery) -> bool | dict:
    """Фильтр: пользователь в workshop.assessment.* стейте SM. Returns dict to avoid double get_intern."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    if not (dispatcher and dispatcher.is_sm_active):
        return False
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return False
    if not (intern.get('current_state') or '').startswith("workshop.assessment."):
        return False
    return {"intern": intern}


@callbacks_router.callback_query(
    F.data.startswith("assess_"),
    _is_in_sm_assessment_state
)
async def cb_assessment_actions(callback: CallbackQuery, state: FSMContext, intern: dict):
    """Assessment callback-ы через SM."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    logger.info(f"[CB] Assessment callback '{callback.data}' for chat_id={callback.message.chat.id}")
    try:
        await dispatcher.route_callback(intern, callback)
    except Exception as e:
        logger.error(f"[CB] Error handling assessment callback: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.try_again', lang))


# === Q&A Feedback: глобальный обработчик (не зависит от стейта) ===

@callbacks_router.callback_query(F.data.startswith("qa_"))
async def cb_qa_feedback(callback: CallbackQuery, state: FSMContext):
    """Обработка feedback-кнопок консультации.

    callback_data форматы:
    - qa_helpful_{qa_id}  → записать helpful=True, убрать кнопки
    - qa_refine_{qa_id}   → загрузить Q&A, re-enter consultation с refinement
    """
    from handlers import get_dispatcher
    from db.queries.qa import get_qa_by_id, update_qa_helpful

    data = callback.data
    chat_id = callback.message.chat.id

    intern = await get_intern(chat_id)
    if not intern:
        await callback.answer()
        return

    lang = intern.get('language', 'ru') or 'ru'

    try:
        if data.startswith("qa_helpful_"):
            # --- 👍 Полезно ---
            qa_id = int(data.split("_")[-1])
            await callback.answer("👍")
            await update_qa_helpful(qa_id, True)
            # Убираем кнопки
            try:
                await callback.message.edit_reply_markup()
            except Exception:
                pass

            # Persistent session: если в консультации — подсказка + кнопка "Завершить"
            current_state_name = intern.get('current_state', '')
            if current_state_name == 'common.consultation':
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                end_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=t('consultation.btn_end_session', lang),
                        callback_data="qa_end_session"
                    )
                ]])
                await callback.message.answer(
                    t('consultation.session_hint', lang),
                    reply_markup=end_kb,
                )

        elif data.startswith("qa_refine_"):
            # --- 🔍 Подробнее ---
            qa_id = int(data.split("_")[-1])
            await callback.answer()

            # Записываем что ответ не помог
            await update_qa_helpful(qa_id, False)

            # Auto-triage (fire-and-forget)
            from core.feedback_triage import triage_feedback
            asyncio.create_task(triage_feedback(qa_id, "not_helpful"))

            # Загружаем оригинальный Q&A
            qa = await get_qa_by_id(qa_id)
            if not qa:
                await callback.message.answer(t('consultation.error', lang))
                return

            # Убираем кнопки с текущего сообщения
            try:
                await callback.message.edit_reply_markup()
            except Exception:
                pass

            # Определяем round: считаем записи с тем же вопросом в текущей сессии
            # (в пределах 5 мин от текущего Q&A, чтобы старые тесты не раздували счётчик)
            from db.queries.qa import get_qa_history
            history = await get_qa_history(chat_id, limit=10)
            qa_time = qa['created_at']
            same_question_recent = sum(
                1 for h in history
                if h['question'] == qa['question']
                and h.get('id') != qa_id
                and abs((h['created_at'] - qa_time).total_seconds()) < 300
            )
            refinement_round = min(same_question_recent + 2, 3)

            # Re-enter consultation с refinement контекстом
            dispatcher = get_dispatcher()
            if dispatcher and dispatcher.is_sm_active:
                await state.clear()
                await dispatcher.go_to(intern, "common.consultation", context={
                    'question': qa['question'],
                    'refinement': True,
                    'previous_answer': qa['answer'],
                    'refinement_round': refinement_round,
                })
            else:
                await callback.message.answer(t('consultation.error', lang))

        elif data.startswith("qa_comment_"):
            # --- ✏️ Замечание ---
            qa_id = int(data.split("_")[-1])
            await callback.answer()

            # Убираем кнопки
            try:
                await callback.message.edit_reply_markup()
            except Exception:
                pass

            # Go to consultation в comment_mode
            dispatcher = get_dispatcher()
            if dispatcher and dispatcher.is_sm_active:
                await state.clear()
                await dispatcher.go_to(intern, "common.consultation", context={
                    'comment_mode': True,
                    'comment_qa_id': qa_id,
                })
            else:
                await callback.message.answer(t('consultation.error', lang))

        elif data == "qa_end_session":
            # --- Завершить сессию консультации ---
            await callback.answer()

            # Убираем кнопки
            try:
                await callback.message.edit_reply_markup()
            except Exception:
                pass

            # Resolve previous state and go_to it
            dispatcher = get_dispatcher()
            if dispatcher and dispatcher.is_sm_active:
                current_state_name = intern.get('current_state', '')
                if current_state_name == 'common.consultation':
                    await state.clear()
                    # Resolve _previous via SM
                    prev_state = dispatcher.sm._previous_states.get(chat_id, 'common.mode_select')
                    await dispatcher.go_to(intern, prev_state)
                else:
                    # Пользователь уже вышел из consultation
                    await callback.message.answer(t('consultation.session_ended', lang))

        else:
            await callback.answer()

    except Exception as e:
        logger.error(f"[CB] Error handling qa feedback: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.answer()


# === Feedback: обратная связь и баг-репорты ===

async def _is_in_sm_feedback_state(callback: CallbackQuery) -> bool | dict:
    """Фильтр: пользователь в utility.feedback стейте SM. Returns dict to avoid double get_intern."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    if not (dispatcher and dispatcher.is_sm_active):
        return False
    intern = await get_intern(callback.message.chat.id)
    if not intern:
        return False
    if intern.get('current_state') != "utility.feedback":
        return False
    return {"intern": intern}


@callbacks_router.callback_query(
    F.data.startswith("feedback:"),
    _is_in_sm_feedback_state
)
async def cb_feedback_actions(callback: CallbackQuery, state: FSMContext, intern: dict):
    """Feedback callback-ы через SM."""
    from handlers import get_dispatcher
    dispatcher = get_dispatcher()

    logger.info(f"[CB] Feedback callback '{callback.data}' for chat_id={callback.message.chat.id}")
    try:
        await dispatcher.route_callback(intern, callback)
    except Exception as e:
        logger.error(f"[CB] Error handling feedback callback: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await callback.answer()
        lang = intern.get('language', 'ru') or 'ru'
        await callback.message.answer(t('errors.try_again', lang))
