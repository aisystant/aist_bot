"""
Стейт: Прохождение теста (Assessment Flow).

Единый стейт для всех фаз опросника:
  intro → questions (12 шт) → self_check → open_question → done

Фазы переключаются внутри стейта через current_context,
а не через SM-переходы. SM видит только flow → result.

Вход: из /assessment или common.mode_select
Выход: workshop.assessment.result (событие "done")
"""

import json
from typing import Optional, Dict

from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from states.base import BaseState
from i18n import t
from db.queries import update_intern
from core.assessment import (
    load_assessment,
    get_question,
    get_total_questions,
    calculate_scores,
    format_progress_bar,
    format_result,
)
from config import get_logger

logger = get_logger(__name__)

# ID теста по умолчанию
DEFAULT_ASSESSMENT = "systematicity"

# Фазы внутри стейта
PHASE_INTRO = "intro"
PHASE_QUESTIONS = "questions"
PHASE_SELF_CHECK = "self_check"
PHASE_OPEN = "open_question"


class AssessmentFlowState(BaseState):
    """
    Стейт прохождения теста.

    Управляет фазами внутри себя: intro → questions → self_check → open → done.
    Прогресс хранится в _user_data (in-memory) по chat_id.
    """

    name = "workshop.assessment.flow"
    display_name = {
        "ru": "Тест: прохождение",
        "en": "Assessment: flow",
        "es": "Evaluación: flujo",
        "fr": "Évaluation: flux",
    }
    allow_global = []  # Assessment не прерывается глобальными событиями

    # In-memory хранение прогресса (chat_id → data)
    _user_data: Dict[int, Dict] = {}

    def _get_lang(self, user) -> str:
        if isinstance(user, dict):
            return user.get('language', 'ru') or 'ru'
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    def _get_data(self, chat_id: int) -> dict:
        """Получить данные прохождения."""
        if chat_id not in self._user_data:
            self._user_data[chat_id] = {
                'phase': PHASE_INTRO,
                'assessment_id': DEFAULT_ASSESSMENT,
                'question_index': 0,
                'answers': {},
            }
        return self._user_data[chat_id]

    # =================================================================
    # ENTER
    # =================================================================

    async def enter(self, user, context: dict = None) -> Optional[str]:
        """Показываем intro с кнопками Начать/Отмена."""
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        # Загружаем тест
        assessment_id = (context or {}).get('assessment_id', DEFAULT_ASSESSMENT)
        assessment = load_assessment(assessment_id)
        if not assessment:
            await self.send(user, t('assessment.not_found', lang))
            return "cancel"

        # Инициализируем данные
        self._user_data[chat_id] = {
            'phase': PHASE_INTRO,
            'assessment_id': assessment_id,
            'question_index': 0,
            'answers': {},
        }

        # Показываем intro
        title = assessment.get('title', {}).get(lang, assessment.get('title', {}).get('ru', ''))
        intro = assessment.get('intro', {}).get(lang, assessment.get('intro', {}).get('ru', ''))
        total = get_total_questions(assessment)

        start_label = t('assessment.btn_start', lang)
        cancel_label = t('assessment.btn_cancel', lang)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=start_label, callback_data="assess_start"),
                InlineKeyboardButton(text=cancel_label, callback_data="assess_cancel"),
            ]
        ])

        await self.send(
            user,
            f"📋 *{title}*\n\n{intro}\n\n_{t('assessment.question_count', lang, count=total)}_",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return None

    # =================================================================
    # HANDLE (text messages)
    # =================================================================

    async def handle(self, user, message: Message) -> Optional[str]:
        """Обработка текстовых сообщений (только для open_question фазы)."""
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)
        data = self._get_data(chat_id)
        text = (message.text or "").strip()

        phase = data.get('phase', PHASE_INTRO)

        # Пропуск открытого вопроса текстом
        if phase == PHASE_OPEN:
            skip_words = ["пропустить", "skip", "saltar", "passer", "/skip"]
            if text.lower() in skip_words:
                data['open_response'] = None
                return "done"

            if len(text) < 10:
                await self.send(user, t('assessment.open_too_short', lang))
                return None

            data['open_response'] = text
            return "done"

        # В остальных фазах текст не ожидается — подсказка
        if phase == PHASE_INTRO:
            await self.send(user, t('assessment.use_buttons', lang))
        elif phase == PHASE_QUESTIONS:
            await self.send(user, t('assessment.use_buttons', lang))
        elif phase == PHASE_SELF_CHECK:
            await self.send(user, t('assessment.use_buttons', lang))

        return None

    # =================================================================
    # HANDLE CALLBACK (inline button presses)
    # =================================================================

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Обработка нажатий inline-кнопок."""
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)
        data = self._get_data(chat_id)
        cb_data = callback.data

        phase = data.get('phase', PHASE_INTRO)

        # --- INTRO ---
        if phase == PHASE_INTRO:
            if cb_data == "assess_start":
                await callback.answer()
                await callback.message.edit_reply_markup()
                data['phase'] = PHASE_QUESTIONS
                data['question_index'] = 0
                data['answers'] = {}
                await self._send_question(user, data, lang)
                return None

            if cb_data == "assess_cancel":
                await callback.answer()
                await callback.message.edit_text(t('assessment.cancelled', lang))
                self._cleanup(chat_id)
                return "cancel"

        # --- QUESTIONS ---
        if phase == PHASE_QUESTIONS:
            if cb_data in ("assess_yes", "assess_no"):
                await callback.answer()
                return await self._process_answer(user, callback, data, lang, cb_data == "assess_yes")

        # --- SELF CHECK ---
        if phase == PHASE_SELF_CHECK:
            if cb_data.startswith("assess_self_"):
                await callback.answer()
                choice = cb_data.replace("assess_self_", "")
                data['self_check'] = choice

                # Edit message to show choice
                assessment = load_assessment(data['assessment_id'])
                self_check = assessment.get('self_check', {})
                options = self_check.get('options', [])
                chosen_label = choice
                for opt in options:
                    if opt['id'] == choice:
                        chosen_label = opt.get('label', {}).get(lang, opt.get('label', {}).get('ru', choice))
                        break

                await callback.message.edit_text(
                    f"✅ {t('assessment.self_check_answer', lang)}: {chosen_label}"
                )

                # Переходим к открытому вопросу
                data['phase'] = PHASE_OPEN
                await self._send_open_question(user, data, lang)
                return None

        await callback.answer()
        return None

    # =================================================================
    # EXIT
    # =================================================================

    async def exit(self, user) -> dict:
        """Передаём данные в result state."""
        chat_id = self._get_chat_id(user)
        data = self._user_data.get(chat_id, {})
        return {
            'assessment_id': data.get('assessment_id', DEFAULT_ASSESSMENT),
            'answers': data.get('answers', {}),
            'self_check': data.get('self_check'),
            'open_response': data.get('open_response'),
        }

    # =================================================================
    # INTERNAL METHODS
    # =================================================================

    async def _process_answer(
        self, user, callback: CallbackQuery, data: dict, lang: str, answer: bool
    ) -> Optional[str]:
        """Обработать ответ Да/Нет на вопрос."""
        assessment = load_assessment(data['assessment_id'])
        if not assessment:
            return "cancel"

        qi = data['question_index']
        question = get_question(assessment, qi)
        if not question:
            return "cancel"

        # Записываем ответ
        data['answers'][question['id']] = answer

        # Edit текущее сообщение — показать ответ
        total = get_total_questions(assessment)
        q_text = question.get('text', {}).get(lang, question.get('text', {}).get('ru', ''))
        answer_text = t('assessment.answer_yes', lang) if answer else t('assessment.answer_no', lang)

        await callback.message.edit_text(
            f"✅ {t('assessment.question_label', lang)} {qi + 1} {t('assessment.of', lang)} {total}\n\n"
            f"{q_text}\n→ {answer_text}"
        )

        # Следующий вопрос или переход к результатам
        next_qi = qi + 1
        if next_qi < total:
            data['question_index'] = next_qi
            await self._send_question(user, data, lang)
            return None
        else:
            # Все вопросы отвечены — показываем промежуточный результат + self check
            scores = calculate_scores(assessment, data['answers'])
            result_text = format_result(assessment, scores, lang)
            await self.send(user, result_text, parse_mode="Markdown")

            # Переходим к self-check
            data['phase'] = PHASE_SELF_CHECK
            await self._send_self_check(user, data, lang)
            return None

    async def _send_question(self, user, data: dict, lang: str) -> None:
        """Отправить вопрос с inline-кнопками Да/Нет."""
        assessment = load_assessment(data['assessment_id'])
        qi = data['question_index']
        question = get_question(assessment, qi)
        total = get_total_questions(assessment)

        if not question:
            return

        q_text = question.get('text', {}).get(lang, question.get('text', {}).get('ru', ''))
        progress = format_progress_bar(qi + 1, total)

        yes_label = t('assessment.btn_yes', lang)
        no_label = t('assessment.btn_no', lang)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=yes_label, callback_data="assess_yes"),
                InlineKeyboardButton(text=no_label, callback_data="assess_no"),
            ]
        ])

        await self.send(
            user,
            f"{t('assessment.question_label', lang)} {qi + 1} {t('assessment.of', lang)} {total}  {progress}\n\n{q_text}",
            reply_markup=keyboard,
        )

    async def _send_self_check(self, user, data: dict, lang: str) -> None:
        """Отправить вопрос самооценки с inline-кнопками."""
        assessment = load_assessment(data['assessment_id'])
        self_check = assessment.get('self_check', {})

        question_text = self_check.get('question', {}).get(
            lang, self_check.get('question', {}).get('ru', '')
        )
        options = self_check.get('options', [])

        buttons = []
        for opt in options:
            label = opt.get('label', {}).get(lang, opt.get('label', {}).get('ru', opt['id']))
            buttons.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"assess_self_{opt['id']}",
                )
            ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await self.send(
            user,
            f"🪞 {question_text}",
            reply_markup=keyboard,
        )

    async def _send_open_question(self, user, data: dict, lang: str) -> None:
        """Отправить открытый вопрос."""
        assessment = load_assessment(data['assessment_id'])
        open_q = assessment.get('open_question', {})

        question_text = open_q.get('text', {}).get(
            lang, open_q.get('text', {}).get('ru', '')
        )
        is_optional = open_q.get('optional', False)

        skip_hint = ""
        if is_optional:
            skip_hint = f"\n\n_{t('assessment.open_skip_hint', lang)}_"

        await self.send(
            user,
            f"✍️ {question_text}{skip_hint}",
            parse_mode="Markdown",
        )

    def _cleanup(self, chat_id: int) -> None:
        """Очистить данные прохождения."""
        self._user_data.pop(chat_id, None)
