"""
Стейт: Результат теста (Assessment Result).

Вход: из workshop.assessment.flow (событие "done")
Выход: common.mode_select (событие "back")

Показывает финальный результат, сохраняет в БД
и обновляет профиль пользователя.
"""

from typing import Optional

from aiogram.types import Message

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

    Сохраняет в таблицу assessments и обновляет profil пользователя.
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
        dominant_emoji = dominant.get('emoji', '')
        dominant_title = dominant.get('title', {}).get(lang, dominant.get('title', {}).get('ru', ''))

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

            # Обновляем профиль
            from db.queries.users import moscow_today
            await update_intern(
                chat_id,
                assessment_state=dominant_id,
                assessment_date=moscow_today(),
            )

            logger.info(
                f"Assessment saved for user {chat_id}: "
                f"dominant={dominant_id}, self_check={self_check}"
            )
        except Exception as e:
            logger.error(f"Error saving assessment for user {chat_id}: {e}")

        # Форматируем результат
        result = format_result(assessment, scores, lang)

        # Дата
        from db.queries.users import moscow_today
        date_str = moscow_today().strftime("%d.%m.%Y")

        # Собираем финальное сообщение
        lines = [
            f"✅ *{t('assessment.completed', lang)}*\n",
            result,
        ]

        if self_check_label:
            lines.append(f"\n🪞 {t('assessment.self_check_label', lang)}: {self_check_label}")

        if open_response:
            # Показываем начало текста
            preview = open_response[:100]
            if len(open_response) > 100:
                preview += "..."
            lines.append(f"\n✍️ {t('assessment.open_response_label', lang)}: _{preview}_")

        lines.append(f"\n📅 {date_str}")
        lines.append(f"\n_{t('assessment.retake_hint', lang)}_")

        try:
            await self.send(user, "\n".join(lines), parse_mode="Markdown")
        except Exception:
            # Fallback без Markdown если символы ломают форматирование
            await self.send(user, "\n".join(lines).replace("*", "").replace("_", ""))

        # Очистка данных flow-стейта
        from states.workshops.assessment.flow import AssessmentFlowState
        AssessmentFlowState._user_data.pop(chat_id, None)

        return "back"

    async def handle(self, user, message: Message) -> Optional[str]:
        """Result — транзитный стейт, не обрабатывает сообщения."""
        return "back"
