"""
Legacy fallback handler — обработка сообщений вне FSM.

Вызывается из handlers/fallback.py когда State Machine неактивен.
"""

import logging

from aiogram.types import Message
from aiogram.fsm.context import FSMContext

from db.queries import get_intern, update_intern, get_topics_today
from i18n import t, detect_language
from core.intent import detect_intent, IntentType
from engines.shared import handle_question, ProcessingStage
from integrations.telegram.keyboards import progress_bar

logger = logging.getLogger(__name__)


def _bot_imports():
    """Lazy imports to avoid circular imports."""
    from core.topics import (
        get_marathon_day, get_topic_title, get_total_topics,
        get_practice_for_day, has_pending_practice,
        has_pending_theory, was_theory_sent_today,
        save_answer, TOPICS,
    )
    from db.queries.users import moscow_today
    from config import BLOOM_AUTO_UPGRADE_AFTER
    from bot import state_machine
    return {
        'get_marathon_day': get_marathon_day,
        'get_topic_title': get_topic_title,
        'get_total_topics': get_total_topics,
        'get_practice_for_day': get_practice_for_day,
        'has_pending_practice': has_pending_practice,
        'has_pending_theory': has_pending_theory,
        'was_theory_sent_today': was_theory_sent_today,
        'save_answer': save_answer,
        'moscow_today': moscow_today,
        'TOPICS': TOPICS,
        'BLOOM_AUTO_UPGRADE_AFTER': BLOOM_AUTO_UPGRADE_AFTER,
        'state_machine': state_machine,
    }


async def legacy_on_unknown_message(message: Message, state: FSMContext):
    """Legacy обработка сообщений вне FSM."""
    from handlers.legacy.learning import LearningStates, on_answer, on_work_product, on_bonus_answer

    chat_id = message.chat.id
    text = message.text or ''

    current_state = await state.get_state()
    logger.info(f"[UNKNOWN] on_unknown_message вызван для chat_id={chat_id}, state={current_state}, text={text[:50] if text else '[no text]'}")

    # Если пользователь в каком-то состоянии — пробуем обработать вручную
    if current_state:
        logger.warning(f"[UNKNOWN] Message in state {current_state} reached fallback. Attempting manual routing for chat_id={chat_id}")
        intern = await get_intern(chat_id)
        lang = intern.get('language', 'ru') if intern else 'ru'
        logger.info(f"[UNKNOWN] Expected states: answer={LearningStates.waiting_for_answer.state}, work={LearningStates.waiting_for_work_product.state}, bonus={LearningStates.waiting_for_bonus_answer.state}")

        try:
            if current_state == LearningStates.waiting_for_answer.state:
                logger.info(f"[UNKNOWN] Routing to on_answer for chat_id={chat_id}")
                await on_answer(message, state, message.bot)
                return
            elif current_state == LearningStates.waiting_for_work_product.state:
                logger.info(f"[UNKNOWN] Routing to on_work_product for chat_id={chat_id}")
                await on_work_product(message, state)
                return
            elif current_state == LearningStates.waiting_for_bonus_answer.state:
                logger.info(f"[UNKNOWN] Routing to on_bonus_answer for chat_id={chat_id}")
                await on_bonus_answer(message, state, message.bot)
                return
        except Exception as e:
            logger.error(f"[UNKNOWN] Error routing to handler: {e}")
            import traceback
            logger.error(traceback.format_exc())
            await message.answer(t('fsm.error_try_learn', lang))
            return

        # Проверяем консультацию
        b = _bot_imports()
        text = message.text or ''
        if text.startswith('?') and b['state_machine'] is not None:
            intern = await get_intern(chat_id)
            if intern:
                await state.clear()
                await b['state_machine'].go_to(intern, "common.consultation", context={'question': text[1:].strip()})
                return

        if 'OnboardingStates' in current_state:
            await message.answer(t('fsm.unrecognized_onboarding', lang))
            return
        elif 'UpdateStates' in current_state:
            await message.answer(t('fsm.unrecognized_update', lang))
            return
        elif 'FeedStates' in current_state:
            await message.answer(t('fsm.unrecognized_feed', lang))
            return
        elif 'MarathonSettingsStates' in current_state:
            await message.answer(t('fsm.enter_time_format', lang))
            return

        logger.warning(f"[UNKNOWN] Unknown state {current_state} for chat_id={chat_id}")
        intern = await get_intern(chat_id)
        lang = intern.get('language', 'ru') if intern else 'ru'
        await message.answer(
            f"Состояние: {current_state}\n\n"
            f"{t('commands.learn', lang)}\n"
            f"{t('commands.progress', lang)}\n"
            f"{t('commands.help', lang)}"
        )
        return

    # Пользователь не в FSM-состоянии
    logger.info(f"[UNKNOWN] Пользователь {chat_id} не в FSM-состоянии, проверяем intent")
    intern = await get_intern(chat_id)

    if not intern:
        lang = detect_language(message.from_user.language_code if message.from_user else None)
        await message.answer(t('fsm.new_user_start', lang))
        return

    lang = intern.get('language', 'ru') or 'ru'

    is_explicit_question = text.strip().startswith('?')
    question_text = text.strip()[1:].strip() if is_explicit_question else text

    # Fallback для режима марафона (восстановление после потери FSM state)
    if intern.get('mode') == 'marathon' and intern.get('onboarding_completed') and not is_explicit_question:
        b = _bot_imports()
        # 1. Незавершённый урок
        theory = b['has_pending_theory'](intern)
        if theory and b['was_theory_sent_today'](intern):
            theory_index, theory_topic = theory
            if text and not text.startswith('/') and len(text.strip()) >= 20:
                logger.info(f"[Fallback] Accepting message as theory answer for user {chat_id}, theory {theory_index}")

                await b['save_answer'](chat_id, theory_index, f"[fallback] {text.strip()}")

                completed = intern['completed_topics'] + [theory_index]
                topics_at_bloom = intern['topics_at_current_bloom'] + 1
                bloom_level = intern['bloom_level']

                level_upgraded = False
                if topics_at_bloom >= b['BLOOM_AUTO_UPGRADE_AFTER'] and bloom_level < 3:
                    bloom_level += 1
                    topics_at_bloom = 0
                    level_upgraded = True

                today = b['moscow_today']()
                topics_today = get_topics_today(intern) + 1

                await update_intern(
                    chat_id,
                    completed_topics=completed,
                    current_topic_index=theory_index + 1,
                    bloom_level=bloom_level,
                    topics_at_current_bloom=topics_at_bloom,
                    topics_today=topics_today,
                    last_topic_date=today
                )

                done = len(completed)
                total = b['get_total_topics']()

                upgrade_msg = ""
                if level_upgraded:
                    upgrade_msg = f"\n\n🎉 *{t('marathon.level_up', lang)}* *{t(f'bloom.level_{bloom_level}_short', lang)}*!"

                updated_intern = {**intern, 'completed_topics': completed}
                practice = b['get_practice_for_day'](updated_intern, theory_topic['day'])

                if practice:
                    practice_index, practice_topic = practice
                    await message.answer(
                        f"✅ *{t('marathon.topic_completed', lang)}*{upgrade_msg}\n\n"
                        f"{progress_bar(done, total)}\n\n"
                        f"⏳ {t('marathon.loading_practice', lang)}",
                        parse_mode="Markdown"
                    )
                    await update_intern(chat_id, current_topic_index=practice_index)
                    task_text = practice_topic.get('task', '')
                    work_product = practice_topic.get('work_product', '')
                    await message.answer(
                        f"📝 *{t('marathon.day_practice', lang, day=practice_topic.get('day', ''))}*\n"
                        f"*{b['get_topic_title'](practice_topic, lang)}*\n\n"
                        f"📋 *{t('marathon.task', lang)}:*\n{task_text}\n\n"
                        f"🎯 *{t('marathon.work_product', lang)}:* {work_product}\n\n"
                        f"💬 *{t('marathon.waiting_for', lang)}:* {t('marathon.work_product_name', lang)}\n"
                        f"_{t('marathon.question_hint', lang)}_",
                        parse_mode="Markdown"
                    )
                else:
                    await message.answer(
                        f"✅ *{t('marathon.topic_completed', lang)}*{upgrade_msg}\n\n"
                        f"{progress_bar(done, total)}\n\n"
                        f"✅ {t('marathon.day_complete', lang)}",
                        parse_mode="Markdown"
                    )
                return

        # 2. Незавершённая практика
        practice = b['has_pending_practice'](intern)
        if practice:
            practice_index, practice_topic = practice
            if text and not text.startswith('/') and len(text.strip()) >= 3:
                practice_day = practice_topic.get('day', b['get_marathon_day'](intern))
                day_topics = [(i, t_topic) for i, t_topic in enumerate(b['TOPICS']) if t_topic['day'] == practice_day]
                theory_done = any(
                    i in intern['completed_topics']
                    for i, t_topic in day_topics if t_topic.get('type') == 'theory'
                )

                if theory_done:
                    logger.info(f"[Fallback] Accepting message as work product for user {chat_id}, practice {practice_index}")

                    await b['save_answer'](
                        chat_id,
                        practice_index,
                        f"[РП][fallback] {text.strip()}"
                    )

                    completed = intern['completed_topics'] + [practice_index]
                    today = b['moscow_today']()
                    topics_today = get_topics_today(intern) + 1

                    await update_intern(
                        chat_id,
                        completed_topics=completed,
                        current_topic_index=practice_index + 1,
                        topics_today=topics_today,
                        last_topic_date=today
                    )

                    done = len(completed)
                    total = b['get_total_topics']()

                    await message.answer(
                        f"✅ *{t('marathon.practice_accepted', lang)}*\n\n"
                        f"📝 РП: {text.strip()[:100]}{'...' if len(text.strip()) > 100 else ''}\n\n"
                        f"{progress_bar(done, total)}\n\n"
                        f"✅ {t('marathon.day_complete', lang)}",
                        parse_mode="Markdown"
                    )
                    return

    # Определяем намерение пользователя
    if is_explicit_question:
        intent_is_question = True
    else:
        intent = detect_intent(text, context={'mode': intern.get('mode')})
        intent_is_question = intent.type == IntentType.QUESTION

    if intent_is_question:
        progress_msg = await message.answer(t('loading.progress.analyzing', lang))

        async def update_progress(stage: str, percent: int):
            stage_texts = {
                ProcessingStage.ANALYZING: t('loading.progress.analyzing', lang),
                ProcessingStage.SEARCHING: t('loading.progress.searching', lang),
                ProcessingStage.GENERATING: t('loading.progress.generating', lang),
                ProcessingStage.DONE: t('loading.progress.done', lang),
            }
            new_text = stage_texts.get(stage, t('loading.processing', lang))
            try:
                await progress_msg.edit_text(new_text)
            except Exception:
                pass

        try:
            answer, sources = await handle_question(
                question=question_text if is_explicit_question else text,
                intern=intern,
                context_topic=None,
                progress_callback=update_progress
            )

            response = answer
            if sources:
                response += "\n\n📚 _Источники: " + ", ".join(sources[:2]) + "_"

            try:
                await progress_msg.delete()
            except Exception:
                pass
            await message.answer(response, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка при обработке вопроса: {e}")
            try:
                await progress_msg.delete()
            except Exception:
                pass
            await message.answer(t('errors.try_again', lang))

    elif not is_explicit_question and intent.type == IntentType.TOPIC_REQUEST:
        await message.answer(
            "Для получения темы используйте /learn"
        )

    else:
        await message.answer(
            t('commands.learn', lang) + "\n" +
            t('commands.progress', lang) + "\n" +
            t('commands.profile', lang) + "\n" +
            t('commands.help', lang)
        )
