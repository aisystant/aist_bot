"""
Стейт: Результат теста (Assessment Result).

Вход: из workshop.assessment.flow (событие "done")
Выход: workshop.marathon.lesson (marathon) | common.settings (settings) | common.mode_select (back)

Показывает финальный результат, сохраняет в БД,
обновляет профиль и предлагает следующий шаг.
"""

from typing import Optional

from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from states.base import BaseState
from i18n import t
from db.queries import update_intern
from db.queries.assessment import save_assessment
from core.assessment import (
    load_assessment,
    calculate_scores,
    get_dominant_group,
    format_result,
)
from config import get_logger

logger = get_logger(__name__)


class AssessmentResultState(BaseState):
    """
    Стейт показа результата теста.

    Сохраняет в таблицу assessments и обновляет профиль пользователя.
    После результата предлагает пройти марафон или заполнить профиль.
    """

    name = "workshop.assessment.result"
    display_name = {
        "ru": "Тест: результат",
        "en": "Assessment: result",
        "es": "Evaluación: resultado",
        "fr": "Évaluation: résultat",
    }
    allow_global = ["consultation", "notes"]

    def _get_lang(self, user) -> str:
        if isinstance(user, dict):
            return user.get('language', 'ru') or 'ru'
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    def _is_profile_sparse(self, user) -> bool:
        """Проверить, заполнен ли профиль содержательно."""
        if isinstance(user, dict):
            get = user.get
        else:
            get = lambda k, d='': getattr(user, k, d)
        motivation = get('motivation', '') or ''
        goals = get('goals', '') or ''
        occupation = get('occupation', '') or ''
        return len(motivation) < 20 or len(goals) < 20 or len(occupation) < 3

    async def enter(self, user, context: dict = None) -> Optional[str]:
        """Показываем итоговый результат и сохраняем."""
        context = context or {}
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        assessment_id = context.get('assessment_id', 'systematicity')
        answers = context.get('answers', {})
        self_check = context.get('self_check')
        open_response = context.get('open_response')

        assessment = load_assessment(assessment_id)
        if not assessment:
            await self.send(user, t('assessment.not_found', lang))
            return "back"

        # Подсчёт
        scores = calculate_scores(assessment, answers)
        dominant = get_dominant_group(assessment, scores)
        dominant_id = dominant.get('id', '')

        # Самооценка — найти label
        self_check_label = self_check or ''
        if self_check and assessment.get('self_check', {}).get('options'):
            for opt in assessment['self_check']['options']:
                if opt['id'] == self_check:
                    self_check_label = opt.get('label', {}).get(
                        lang, opt.get('label', {}).get('ru', self_check)
                    )
                    break

        # Сохраняем в БД
        try:
            await save_assessment(
                chat_id=chat_id,
                assessment_id=assessment_id,
                answers=answers,
                scores=scores,
                dominant_state=dominant_id,
                self_check=self_check,
                open_response=open_response,
            )

            # ЦД: событие assessment_completed (WP-85, WP-151 Ф3: расширенный payload)
            from db.queries.events import log_event
            await log_event(chat_id, 'assessment_completed', {
                'assessment_id': assessment_id,
                'dominant_state': dominant_id,
                'self_check': self_check,
                'scores': scores if scores else None,
            }, confidence=0.9)

            from db.queries.users import moscow_today
            from core.helpers import ASSESSMENT_BLOOM_MAP

            # Auto-init bloom_level из состояния теста
            # (только если пользователь не выставлял вручную, т.е. стоит дефолт 1)
            update_kwargs = dict(
                assessment_state=dominant_id,
                assessment_date=moscow_today(),
            )
            if isinstance(user, dict):
                current_bloom = user.get('bloom_level', 1)
            else:
                current_bloom = getattr(user, 'bloom_level', 1)
            suggested_bloom = ASSESSMENT_BLOOM_MAP.get(dominant_id, 1)
            if current_bloom == 1 or suggested_bloom > current_bloom:
                update_kwargs['bloom_level'] = suggested_bloom

            await update_intern(chat_id, **update_kwargs)

            logger.info(
                f"Assessment saved for user {chat_id}: "
                f"dominant={dominant_id}, self_check={self_check}"
            )
        except Exception as e:
            logger.error(f"Error saving assessment for user {chat_id}: {e}")

        # Форматируем результат
        result = format_result(assessment, scores, lang)

        from db.queries.users import moscow_today
        date_str = moscow_today().strftime("%d.%m.%Y")

        # Собираем финальное сообщение с результатом
        lines = [
            f"✅ *{t('assessment.completed', lang)}*\n",
            result,
        ]

        if self_check_label:
            lines.append(f"\n🪞 {t('assessment.self_check_label', lang)}: {self_check_label}")

        if open_response:
            preview = open_response[:100]
            if len(open_response) > 100:
                preview += "..."
            lines.append(f"\n✍️ {t('assessment.open_response_label', lang)}: _{preview}_")

        lines.append(f"\n📅 {date_str}")

        try:
            await self.send(user, "\n".join(lines), parse_mode="Markdown")
        except Exception:
            await self.send(user, "\n".join(lines).replace("*", "").replace("_", ""))

        # Очистка данных flow-стейта
        from states.workshops.assessment.flow import AssessmentFlowState
        AssessmentFlowState._user_data.pop(chat_id, None)

        # Рекомендация: марафон + профиль
        rec_lines = [t('assessment.recommend_marathon', lang)]
        if self._is_profile_sparse(user):
            rec_lines.append(t('assessment.recommend_profile', lang))

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t('assessment.btn_marathon', lang),
                    callback_data="assess_result_marathon",
                ),
                InlineKeyboardButton(
                    text=t('assessment.btn_profile', lang),
                    callback_data="assess_result_settings",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t('assessment.btn_menu', lang),
                    callback_data="assess_result_menu",
                ),
            ],
        ])

        await self.send(user, "\n".join(rec_lines), reply_markup=keyboard)

        return None  # Остаёмся в стейте — ждём нажатия кнопки

    async def handle(self, user, message: Message) -> Optional[str]:
        """Текстовое сообщение — подсказываем про кнопки."""
        lang = self._get_lang(user)
        await self.send(user, t('assessment.use_buttons', lang))
        return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Обработка нажатий кнопок после результата."""
        await callback.answer()
        data = callback.data

        if data == "assess_result_marathon":
            return "marathon"
        elif data == "assess_result_settings":
            return "settings"
        elif data == "assess_result_menu":
            return "back"

        return None
