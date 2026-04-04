"""
Стейт: Мои данные (/mydata).

Персональный дата-центр пользователя: метрики, прозрачность алгоритмов,
приватность, управление данными, тир-прогрессия.

Ref: DP.D.028 (User Data Tiers), DP.ARCH.002 (Service Tiers).

Вход: по команде /mydata или через меню
Выход: _previous
"""

import json
import logging
from typing import Optional

from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from states.base import BaseState
from i18n import t

logger = logging.getLogger(__name__)

# ─── Tier detection helpers ────────────────────────────────────────────────

TIER_NAMES = {
    'ru': {0: 'T0 — Новый', 1: 'T1 — Старт', 2: 'T2 — Изучение', 3: 'T3 — Персонализация', 4: 'T4 — Созидание', 5: 'T5 — Админ'},
    'en': {0: 'T0 — New', 1: 'T1 — Start', 2: 'T2 — Learning', 3: 'T3 — Personalization', 4: 'T4 — Creation', 5: 'T5 — Admin'},
}

TIER_EMOJI = {0: '⚪', 1: '🟢', 2: '📘', 3: '🧬', 4: '🚀', 5: '⚡'}

# ─── Categories by tier ────────────────────────────────────────────────────

CATEGORIES_T1 = {
    'profile': ['name', 'occupation', 'language', 'experience_level', 'mode'],
    'activity': ['active_days_total', 'active_days_streak', 'longest_streak', 'last_active_date'],
    'marathon': ['theory_answers_count', 'work_products_count', 'qa_count'],
}

CATEGORIES_T2_EXTRA = {
    'profile': ['role', 'domain', 'interests', 'goals', 'motivation', 'study_duration', 'schedule_time'],
    'learning': [
        'marathon_status', 'feed_status', 'current_topic_index',
        'complexity_level', 'assessment_state', 'assessment_date',
    ],
    'feed': ['total_digests', 'total_fixations', 'current_feed_topics'],
    'consultations': ['qa_count'],
}

# Человекочитаемые названия полей
FIELD_LABELS = {
    'ru': {
        'name': 'Имя', 'occupation': 'Профессия', 'role': 'Роль',
        'domain': 'Домен', 'interests': 'Интересы', 'goals': 'Цели',
        'motivation': 'Мотивация', 'language': 'Язык',
        'experience_level': 'Уровень опыта', 'mode': 'Режим',
        'study_duration': 'Время занятий', 'schedule_time': 'Расписание',
        'marathon_status': 'Статус марафона', 'feed_status': 'Статус ленты',
        'current_topic_index': 'Текущая тема (индекс)',
        'complexity_level': 'Уровень сложности',
        'assessment_state': 'Результат теста', 'assessment_date': 'Дата теста',
        'active_days_total': 'Всего активных дней',
        'active_days_streak': 'Текущая серия', 'longest_streak': 'Макс. серия',
        'last_active_date': 'Последняя активность',
        'theory_answers_count': 'Ответов на теорию',
        'work_products_count': 'Рабочих продуктов', 'qa_count': 'Вопросов консультанту',
        'total_digests': 'Дайджестов', 'total_fixations': 'Фиксаций',
        'current_feed_topics': 'Текущие темы',
    },
    'en': {
        'name': 'Name', 'occupation': 'Occupation', 'role': 'Role',
        'domain': 'Domain', 'interests': 'Interests', 'goals': 'Goals',
        'motivation': 'Motivation', 'language': 'Language',
        'experience_level': 'Experience level', 'mode': 'Mode',
        'study_duration': 'Study duration', 'schedule_time': 'Schedule',
        'marathon_status': 'Marathon status', 'feed_status': 'Feed status',
        'current_topic_index': 'Current topic (index)',
        'complexity_level': 'Complexity level',
        'assessment_state': 'Assessment result', 'assessment_date': 'Assessment date',
        'active_days_total': 'Total active days',
        'active_days_streak': 'Current streak', 'longest_streak': 'Longest streak',
        'last_active_date': 'Last active date',
        'theory_answers_count': 'Theory answers',
        'work_products_count': 'Work products', 'qa_count': 'Consultant questions',
        'total_digests': 'Digests', 'total_fixations': 'Fixations',
        'current_feed_topics': 'Current topics',
    },
}


class MyDataState(BaseState):
    """
    Персональный дата-центр пользователя.

    Хаб → 5 секций:
      📊 Мои метрики — данные по категориям (tier-aware)
      🎯 Как это работает — прозрачность алгоритмов
      🔒 Приватность — протокол хранения
      🗑 Управление данными — удаление
      🏆 Мой уровень — тир-прогрессия
    """

    name = "utility.mydata"
    display_name = {
        "ru": "Мои данные", "en": "My Data",
        "es": "Mis datos", "fr": "Mes données", "zh": "我的数据",
    }
    allow_global = ["consultation", "notes"]

    # ─── Helpers ────────────────────────────────────────────────────────

    def _get_lang(self, user) -> str:
        if isinstance(user, dict):
            return user.get('language', 'ru')
        return getattr(user, 'language', 'ru') or 'ru'

    def _get_chat_id(self, user) -> int:
        if isinstance(user, dict):
            return user.get('chat_id')
        return getattr(user, 'chat_id', None)

    def _get_user_name(self, user) -> str:
        if isinstance(user, dict):
            return user.get('name', '')
        return getattr(user, 'name', '') or ''

    def _field_label(self, field: str, lang: str) -> str:
        labels = FIELD_LABELS.get(lang, FIELD_LABELS['en'])
        return labels.get(field, field)

    def _format_value(self, value) -> str:
        if value is None:
            return "—"
        # JSON arrays stored as TEXT (e.g. interests)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return ", ".join(str(v) for v in parsed) if parsed else "—"
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        if isinstance(value, list):
            return ", ".join(str(v) for v in value) if value else "—"
        if hasattr(value, 'strftime'):
            return value.strftime('%d.%m.%Y')
        return str(value)

    async def _get_profile(self, chat_id: int) -> Optional[dict]:
        from db.queries.profile import get_knowledge_profile
        return await get_knowledge_profile(chat_id)

    async def _detect_tier(self, chat_id: int) -> int:
        """Определить текущий тир пользователя (единый детектор).

        Делегирует в core.tier_detector.detect_ui_tier().
        """
        from core.tier_detector import detect_ui_tier
        return await detect_ui_tier(chat_id)

    def _get_categories_for_tier(self, tier: int) -> dict:
        """Собрать категории данных для тира."""
        categories = {}
        # T1 — базовые
        for cat, fields in CATEGORIES_T1.items():
            categories[cat] = list(fields)
        # T2+ — расширенные
        if tier >= 2:
            for cat, fields in CATEGORIES_T2_EXTRA.items():
                if cat in categories:
                    categories[cat].extend(fields)
                else:
                    categories[cat] = list(fields)
        return categories

    # ─── Entry: Hub ────────────────────────────────────────────────────

    async def enter(self, user, context: dict = None) -> None:
        """Показать хаб дата-центра."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        tier = await self._detect_tier(chat_id)
        tier_name = TIER_NAMES.get(lang, TIER_NAMES['en']).get(tier, f'T{tier}')

        text = f"*{t('mydata.title', lang)}*\n"
        text += f"{t('mydata.summary', lang)}\n\n"
        text += f"{TIER_EMOJI[tier]} {t('mydata.your_tier', lang)}: *{tier_name}*\n\n"
        text += f"📊 *{t('mydata.sec_metrics', lang)}* — {t('mydata.sec_metrics_desc', lang)}\n"
        text += f"📈 *{t('mydata.sec_activity', lang)}* — {t('mydata.sec_activity_desc', lang)}\n"
        text += f"🔒 *{t('mydata.sec_privacy', lang)}* — {t('mydata.sec_privacy_desc', lang)}\n"
        text += f"🏆 *{t('mydata.sec_tiers', lang)}* — {t('mydata.sec_tiers_desc', lang)}\n"
        text += f"🎯 *{t('mydata.sec_how', lang)}* — {t('mydata.sec_how_desc', lang)}\n"
        text += f"🔄 *{t('mydata.sec_manage', lang)}* — {t('mydata.sec_manage_desc', lang)}\n"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📊 {t('mydata.sec_metrics', lang)}",
                    callback_data="mydata_sec_metrics",
                ),
                InlineKeyboardButton(
                    text=f"📈 {t('mydata.sec_activity', lang)}",
                    callback_data="mydata_sec_activity",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"🔒 {t('mydata.sec_privacy', lang)}",
                    callback_data="mydata_sec_privacy",
                ),
                InlineKeyboardButton(
                    text=f"🏆 {t('mydata.sec_tiers', lang)}",
                    callback_data="mydata_sec_tiers",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"🎯 {t('mydata.sec_how', lang)}",
                    callback_data="mydata_sec_how",
                ),
                InlineKeyboardButton(
                    text=f"🔄 {t('mydata.sec_manage', lang)}",
                    callback_data="mydata_sec_manage",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t('buttons.back', lang),
                    callback_data="mydata_back",
                ),
            ],
        ])

        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    # ─── Text input (for delete confirmation) ──────────────────────────

    async def handle(self, user, message: Message) -> Optional[str]:
        """Обработка текстового ввода — только для подтверждения удаления."""
        chat_id = self._get_chat_id(user)
        lang = self._get_lang(user)

        # Проверяем, ожидаем ли подтверждение удаления
        context = await self._get_context(chat_id)
        if not context or not context.get('awaiting_delete'):
            return None

        text = (message.text or '').strip()
        expected = t('mydata.delete_confirm_phrase', lang)

        if text == expected:
            await self._execute_delete(user, chat_id, lang)
            return "deleted"
        else:
            # Не совпало
            await self.send(
                user,
                t('mydata.delete_mismatch', lang),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"← {t('mydata.back_to_hub', lang)}",
                        callback_data="mydata_hub",
                    )],
                ]),
            )
            await self._clear_context(chat_id)
            return None

    # ─── Callback routing ──────────────────────────────────────────────

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        data = callback.data
        await callback.answer()

        if data == "mydata_back":
            return "back"

        if data == "mydata_hub":
            await callback.message.delete()
            await self.enter(user)
            return None

        # ── Sections ──
        if data == "mydata_sec_metrics":
            await self._show_metrics_hub(user, callback)
            return None

        if data == "mydata_sec_activity":
            await self._show_activity(user, callback)
            return None

        if data == "mydata_sec_how":
            await self._show_how_it_works(user, callback)
            return None

        if data == "mydata_sec_privacy":
            await self._show_privacy(user, callback)
            return None

        if data == "mydata_privacy_details":
            await self._show_privacy_details(user, callback)
            return None

        if data == "mydata_sec_tiers":
            await self._show_tier_progression(user, callback)
            return None

        if data == "mydata_action_link":
            lang = self._get_lang(user)
            await self.send(user, t('mydata.action_link_hint', lang))
            return None

        if data == "mydata_action_twin":
            lang = self._get_lang(user)
            await self.send(user, t('mydata.action_twin_hint', lang))
            return None

        if data == "mydata_action_github":
            lang = self._get_lang(user)
            await self.send(user, t('mydata.action_github_hint', lang))
            return None

        if data == "mydata_sec_manage":
            await self._show_manage(user, callback)
            return None

        # ── Metrics categories ──
        if data.startswith("mydata_cat_"):
            category = data.replace("mydata_cat_", "")
            await self._show_category(user, category, callback)
            return None

        # ── AI explanations (T2+) ──
        if data.startswith("mydata_why_"):
            category = data.replace("mydata_why_", "")
            await self._explain_category(user, category, "why")
            return None

        if data.startswith("mydata_improve_"):
            category = data.replace("mydata_improve_", "")
            await self._explain_category(user, category, "improve")
            return None

        # ── Manage: confirm actions ──
        if data == "mydata_reset_stats":
            await self._confirm_action(user, callback, "reset_stats")
            return None

        if data == "mydata_reset_marathon":
            await self._confirm_action(user, callback, "reset_marathon")
            return None

        if data == "mydata_clear_qa":
            await self._confirm_action(user, callback, "clear_qa")
            return None

        if data == "mydata_clear_notes":
            await self._confirm_action(user, callback, "clear_notes")
            return None

        if data == "mydata_reset_learning":
            await self._confirm_action(user, callback, "reset_learning")
            return None

        # ── Manage: confirmed actions ──
        if data == "mydata_confirm_reset_stats":
            await self._reset_stats(user)
            return None

        if data == "mydata_confirm_reset_marathon":
            await self._reset_marathon(user)
            return None

        if data == "mydata_confirm_clear_qa":
            await self._clear_qa(user)
            return None

        if data == "mydata_confirm_clear_notes":
            await self._clear_notes(user)
            return None

        if data == "mydata_confirm_reset_learning":
            await self._reset_learning(user)
            return None

        if data == "mydata_disconnect_github":
            await self._disconnect_github(user)
            return None

        if data == "mydata_delete_all":
            await self._start_delete_flow(user, callback)
            return None

        if data == "mydata_cancel_delete":
            chat_id = self._get_chat_id(user)
            await self._clear_context(chat_id)
            await callback.message.delete()
            await self.enter(user)
            return None

        return None

    # ═══ Section: Metrics Hub ══════════════════════════════════════════

    async def _show_metrics_hub(self, user, callback: CallbackQuery) -> None:
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        tier = await self._detect_tier(chat_id)
        profile = await self._get_profile(chat_id)

        if not profile:
            await self.send(user, t('mydata.no_data', lang))
            return

        text = f"*📊 {t('mydata.sec_metrics', lang)}*\n\n"

        # Краткая сводка
        text += f"👤 {t('mydata.cat_profile', lang)}: {profile.get('name', '—')}, {profile.get('occupation', '—')}\n"

        streak = profile.get('active_days_streak', 0)
        total = profile.get('active_days_total', 0)
        text += f"🔥 {t('mydata.cat_activity', lang)}: {streak} {t('mydata.streak', lang)}, {total} {t('mydata.total', lang)}\n"

        wp = profile.get('work_products_count', 0)
        theory = profile.get('theory_answers_count', 0)
        text += f"🏃 {t('mydata.cat_marathon', lang)}: {wp} {t('mydata.wp', lang)}, {theory} {t('mydata.answers', lang)}\n"

        if tier >= 2:
            digests = profile.get('total_digests', 0)
            fixations = profile.get('total_fixations', 0)
            text += f"📖 {t('mydata.cat_feed', lang)}: {digests} {t('mydata.digests', lang)}, {fixations} {t('mydata.fixations', lang)}\n"

            qa = profile.get('qa_count', 0)
            text += f"💬 {t('mydata.cat_consultations', lang)}: {qa}\n"

        # Кнопки категорий
        buttons = [
            [
                InlineKeyboardButton(text=f"👤 {t('mydata.cat_profile', lang)}", callback_data="mydata_cat_profile"),
                InlineKeyboardButton(text=f"🔥 {t('mydata.cat_activity', lang)}", callback_data="mydata_cat_activity"),
            ],
            [InlineKeyboardButton(text=f"🏃 {t('mydata.cat_marathon', lang)}", callback_data="mydata_cat_marathon")],
        ]

        if tier >= 2:
            buttons[1].append(
                InlineKeyboardButton(text=f"📖 {t('mydata.cat_feed', lang)}", callback_data="mydata_cat_feed")
            )
            buttons.append([
                InlineKeyboardButton(text=f"📚 {t('mydata.cat_learning', lang)}", callback_data="mydata_cat_learning"),
                InlineKeyboardButton(text=f"💬 {t('mydata.cat_consultations', lang)}", callback_data="mydata_cat_consultations"),
            ])

        buttons.append([InlineKeyboardButton(
            text=f"← {t('mydata.back_to_hub', lang)}", callback_data="mydata_hub",
        )])

        try:
            await callback.message.edit_text(
                text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="Markdown",
            )
        except Exception:
            await self.send(
                user, text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="Markdown",
            )

    # ═══ Section: Category detail ══════════════════════════════════════

    async def _show_category(self, user, category: str, callback: CallbackQuery) -> None:
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        tier = await self._detect_tier(chat_id)
        profile = await self._get_profile(chat_id)

        if not profile:
            await self.send(user, t('mydata.no_data', lang))
            return

        categories = self._get_categories_for_tier(tier)
        fields = categories.get(category, [])

        # DT fields for T3 profile
        dt_data = None
        if tier >= 3 and category == 'profile':
            dt_data = await self._get_dt_profile(chat_id)

        cat_label = t(f'mydata.cat_{category}', lang)
        text = f"*{cat_label}*\n\n"

        for field in fields:
            label = self._field_label(field, lang)
            value = self._format_value(profile.get(field))
            text += f"• {label}: {value}\n"

        # DT data for T3+
        if dt_data:
            text += f"\n*{t('mydata.dt_section', lang)}*\n"
            for key, value in dt_data.items():
                text += f"• {key}: {self._format_value(value)}\n"

        # Buttons: Why + Improve (T2+) or just back
        buttons = []
        if tier >= 2:
            buttons.append([
                InlineKeyboardButton(
                    text=f"❓ {t('mydata.why_button', lang)}",
                    callback_data=f"mydata_why_{category}",
                ),
                InlineKeyboardButton(
                    text=f"📈 {t('mydata.improve_button', lang)}",
                    callback_data=f"mydata_improve_{category}",
                ),
            ])
        buttons.append([InlineKeyboardButton(
            text=f"← {t('mydata.back_to_metrics', lang)}",
            callback_data="mydata_sec_metrics",
        )])

        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="Markdown",
            )
        except Exception:
            await self.send(
                user, text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="Markdown",
            )

    async def _get_dt_profile(self, chat_id: int) -> Optional[dict]:
        """Получить данные из ЦД для T3+."""
        try:
            from clients.digital_twin import digital_twin
            if not digital_twin.is_connected(chat_id):
                return None
            profile = await digital_twin.get_user_profile(chat_id)
            if not profile:
                return None
            # Извлечь ключевые поля
            result = {}
            if profile.get('degree'):
                result['Degree'] = profile['degree']
            if profile.get('stage'):
                result['Stage'] = profile['stage']
            indicators = profile.get('indicators', {})
            pref = indicators.get('IND.1.PREF', {})
            if pref.get('objective'):
                result['Objective'] = pref['objective']
            if pref.get('role_set'):
                result['Roles'] = ', '.join(pref['role_set'])
            if pref.get('weekly_time_budget'):
                result['Time Budget'] = f"{pref['weekly_time_budget']}h/week"
            return result  # {} = connected but empty profile
        except Exception as e:
            logger.warning(f"DT profile fetch failed: {e}")
            return None

    # ═══ Section: AI Explanation ════════════════════════════════════════

    async def _explain_category(self, user, category: str, mode: str) -> None:
        """Claude L2 объясняет 'почему' или 'как улучшить'."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        tier = await self._detect_tier(chat_id)

        # T2+ gate
        if tier < 2:
            from core.access import access_layer
            text, keyboard = await access_layer.get_paywall("consultation", lang)
            await self.send(user, text, reply_markup=keyboard)
            return

        profile = await self._get_profile(chat_id)
        if not profile:
            await self.send(user, t('mydata.no_data', lang))
            return

        await self.send(user, f"⏳ {t('mydata.why_thinking', lang)}")

        categories = self._get_categories_for_tier(tier)
        fields = categories.get(category, [])
        cat_data = {f: self._format_value(profile.get(f)) for f in fields}
        cat_label = t(f'mydata.cat_{category}', lang)

        lang_instruction = {
            'ru': "Отвечай на русском. Будь дружелюбным и конкретным.",
            'en': "Answer in English. Be friendly and specific.",
        }.get(lang, "Answer in English.")

        if mode == "why":
            task = f"""Объясни в 3-5 предложениях, откуда берутся эти данные.
Свяжи числа с действиями пользователя.
Если данные пустые — объясни, что нужно сделать чтобы они появились.
НЕ придумывай данные, которых нет в контексте."""
        else:  # improve
            task = f"""Дай 3-4 конкретных совета, как улучшить эти показатели.
Привяжи каждый совет к конкретному действию в боте (команда, кнопка).
Если показатель уже хороший — похвали и предложи следующий уровень.
НЕ давай общих советов типа 'занимайся больше'."""

        system_prompt = f"""Ты — AIST Bot, бот-наставник для систематического обучения.
Пользователь смотрит свои данные в категории «{cat_label}».

{lang_instruction}

ДАННЫЕ ПОЛЬЗОВАТЕЛЯ:
{json.dumps(cat_data, ensure_ascii=False, indent=2)}

ЗАДАНИЕ:
{task}"""

        from clients import claude
        from config import CLAUDE_MODEL_HAIKU
        try:
            answer = await claude.generate(
                system_prompt=system_prompt,
                user_prompt=f"{'Объясни' if mode == 'why' else 'Как улучшить'} мои данные в категории «{cat_label}»",
                max_tokens=1000, model=CLAUDE_MODEL_HAIKU,
            )
        except Exception as e:
            logger.error(f"MyData explain error: {e}")
            answer = t('mydata.explain_error', lang)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"← {t('mydata.back_to_metrics', lang)}",
                callback_data="mydata_sec_metrics",
            )],
        ])

        try:
            await self.send(user, answer, reply_markup=keyboard, parse_mode="Markdown")
        except Exception:
            await self.send(user, answer, reply_markup=keyboard)

    # ═══ Section: Activity (twin insights) ══════════════════════════════

    async def _show_activity(self, user, callback: CallbackQuery) -> None:
        """Показать AI-анализ активности из Digital Twin."""
        from handlers.twin import _handle_insights
        from db.queries import get_intern

        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        intern = await get_intern(chat_id)

        await _handle_insights(callback.message, intern, lang)

    # ═══ Section: How it works ═════════════════════════════════════════

    async def _show_how_it_works(self, user, callback: CallbackQuery) -> None:
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        tier = await self._detect_tier(chat_id)
        profile = await self._get_profile(chat_id)

        text = f"*🎯 {t('mydata.sec_how', lang)}*\n\n"
        text += t('mydata.how_intro', lang) + "\n\n"

        # Показать что попадает в промпт Claude (конкретные данные пользователя)
        text += f"*{t('mydata.how_prompt_title', lang)}*\n"

        if profile:
            text += f"1. 📋 {t('mydata.how_profile', lang)}:\n"
            text += f"   → occupation: {profile.get('occupation', '—')}\n"
            text += f"   → complexity: {profile.get('complexity_level', 1)}\n"

            if tier >= 2:
                interests = profile.get('interests')
                if interests:
                    # Parse JSON TEXT → list
                    if isinstance(interests, str):
                        try:
                            interests = json.loads(interests)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass
                    if isinstance(interests, list):
                        interests = ', '.join(str(i) for i in interests[:3])
                    text += f"   → interests: {interests}\n"
                goals = profile.get('goals', '')
                if goals:
                    text += f"   → goals: {str(goals)[:60]}\n"

            if tier >= 3:
                text += f"\n2. 🧬 {t('mydata.how_dt', lang)}:\n"
                dt = await self._get_dt_profile(chat_id)
                if dt is None:
                    text += f"   → {t('mydata.dt_not_connected', lang)}\n"
                elif dt:
                    for k, v in dt.items():
                        text += f"   → {k}: {v}\n"
                else:
                    text += f"   → {t('mydata.dt_empty_profile', lang)}\n"

        text += f"\n*{t('mydata.how_not_sent', lang)}*\n"
        text += t('mydata.how_not_sent_list', lang)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"← {t('mydata.back_to_hub', lang)}", callback_data="mydata_hub",
            )],
        ])

        try:
            await callback.message.edit_text(
                text, reply_markup=keyboard, parse_mode="Markdown",
            )
        except Exception:
            await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    # ═══ Section: Privacy ══════════════════════════════════════════════

    async def _show_privacy(self, user, callback: CallbackQuery) -> None:
        """Показать приватность: что собираем на ТЕКУЩЕМ тире + кнопка «Подробнее»."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        tier = await self._detect_tier(chat_id)
        tier_name = TIER_NAMES.get(lang, TIER_NAMES['en']).get(tier, f'T{tier}')

        text = f"*🔒 {t('mydata.sec_privacy', lang)}*\n\n"
        text += t('mydata.privacy_your_tier', lang, tier=tier_name) + "\n"
        text += t('mydata.privacy_t1', lang)

        if tier >= 2:
            text += t('mydata.privacy_t2_extra', lang)

        if tier >= 3:
            text += t('mydata.privacy_t3_extra', lang)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"📋 {t('mydata.privacy_details_btn', lang)}",
                callback_data="mydata_privacy_details",
            )],
            [InlineKeyboardButton(
                text=f"← {t('mydata.back_to_hub', lang)}", callback_data="mydata_hub",
            )],
        ])

        try:
            await callback.message.edit_text(
                text, reply_markup=keyboard, parse_mode="Markdown",
            )
        except Exception:
            await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    async def _show_privacy_details(self, user, callback: CallbackQuery) -> None:
        """Полная политика приватности."""
        lang = self._get_lang(user)
        text = f"*🔒 {t('mydata.sec_privacy', lang)}*\n\n"
        text += t('mydata.privacy_text', lang)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"← {t('mydata.back_to_hub', lang)}", callback_data="mydata_sec_privacy",
            )],
        ])

        try:
            await callback.message.edit_text(
                text, reply_markup=keyboard, parse_mode="Markdown",
            )
        except Exception:
            await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    # ═══ Section: Tier Progression ═════════════════════════════════════

    async def _show_tier_progression(self, user, callback: CallbackQuery) -> None:
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        current_tier = await self._detect_tier(chat_id)
        tier_names = TIER_NAMES.get(lang, TIER_NAMES['en'])

        text = f"*🏆 {t('mydata.sec_tiers', lang)}*\n\n"

        tiers_info = {
            'ru': {
                0: ('Новый пользователь. Марафон, тест, прогресс. Trial 30 дней.', 'Привязать аккаунт Aisystant (/link)'),
                1: ('Привязан к Aisystant. Марафон, базовый профиль.', 'Оформить подписку «Бесконечное развитие»'),
                2: ('Подписка. Лента, консультации, заметки, планы.', 'Подключить Цифровой Двойник (/twin)'),
                3: ('ЦД подключён. Персонализация, полный профиль.', 'Подключить GitHub (/github)'),
                4: ('Локальный экзокортекс. Claude Code, агенты, личная база знаний.', 'Установить Claude Code + fork шаблона'),
            },
            'en': {
                0: ('New user. Marathon, test, progress. 30-day trial.', 'Link Aisystant account (/link)'),
                1: ('Linked to Aisystant. Marathon, basic profile.', 'Subscribe to "Infinite Development" on Aisystant'),
                2: ('Subscription. Feed, consultations, notes, plans.', 'Connect Digital Twin (/twin)'),
                3: ('DT connected. Personalization, full profile.', 'Connect GitHub (/github)'),
                4: ('Local exocortex. Claude Code, agents, personal knowledge base.', 'Install Claude Code + fork template'),
            },
        }

        info = tiers_info.get(lang, tiers_info['en'])

        for tier_num in range(0, 5):
            emoji = TIER_EMOJI[tier_num]
            name = tier_names[tier_num]
            desc, how = info[tier_num]

            if tier_num == current_tier:
                text += f"→ *{emoji} {name}* ← {t('mydata.current_tier', lang)}\n"
                text += f"  {desc}\n\n"
            elif tier_num < current_tier:
                text += f"✅ {emoji} {name}\n"
                text += f"  {desc}\n\n"
            else:
                text += f"🔒 {emoji} {name}\n"
                text += f"  {desc}\n"
                text += f"  _{t('mydata.how_to_unlock', lang)}: {how}_\n\n"

        # Action button for next tier (П1: progress bar with CTA)
        buttons = []
        next_tier_action = self._get_next_tier_action(current_tier, lang)
        if next_tier_action:
            btn_text, btn_data = next_tier_action
            buttons.append([InlineKeyboardButton(text=btn_text, callback_data=btn_data)])

        buttons.append([InlineKeyboardButton(
            text=f"← {t('mydata.back_to_hub', lang)}", callback_data="mydata_hub",
        )])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            await callback.message.edit_text(
                text, reply_markup=keyboard, parse_mode="Markdown",
            )
        except Exception:
            await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    def _get_next_tier_action(self, current_tier: int, lang: str) -> tuple[str, str] | None:
        """Return (button_text, callback_data) for the next tier unlock action."""
        actions = {
            'ru': {
                0: ("🔗 Привязать Aisystant", "mydata_action_link"),
                1: ("💳 Оформить подписку «Бесконечное развитие»", "aisystant_subscribe"),
                2: ("🧬 Подключить Цифровой Двойник", "mydata_action_twin"),
                3: ("🔗 Подключить GitHub", "mydata_action_github"),
            },
            'en': {
                0: ("🔗 Link Aisystant", "mydata_action_link"),
                1: ("💳 Subscribe to «Infinite Development»", "aisystant_subscribe"),
                2: ("🧬 Connect Digital Twin", "mydata_action_twin"),
                3: ("🔗 Connect GitHub", "mydata_action_github"),
            },
        }
        tier_actions = actions.get(lang, actions['en'])
        return tier_actions.get(current_tier)

    # ═══ Section: Manage Data ══════════════════════════════════════════

    async def _show_manage(self, user, callback: CallbackQuery) -> None:
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        text = f"*🔄 {t('mydata.sec_manage', lang)}*\n\n"
        text += t('mydata.manage_intro', lang)

        buttons = [
            [InlineKeyboardButton(
                text=t('mydata.btn_reset_stats', lang),
                callback_data="mydata_reset_stats",
            )],
            [InlineKeyboardButton(
                text=t('mydata.btn_reset_marathon', lang),
                callback_data="mydata_reset_marathon",
            )],
            [InlineKeyboardButton(
                text=t('mydata.btn_clear_qa', lang),
                callback_data="mydata_clear_qa",
            )],
            [InlineKeyboardButton(
                text=t('mydata.btn_reset_learning', lang),
                callback_data="mydata_reset_learning",
            )],
        ]

        # GitHub notes clear (if connected)
        try:
            from db.queries.github import get_github_connection
            gh = await get_github_connection(chat_id)
            if gh:
                buttons.append([InlineKeyboardButton(
                    text=t('mydata.btn_clear_notes', lang),
                    callback_data="mydata_clear_notes",
                )])
        except Exception:
            pass

        # Удаление аккаунта — внизу секции, отдельно от сбросов
        buttons.append([InlineKeyboardButton(
            text=f"⚠️ {t('mydata.btn_delete_all', lang)}",
            callback_data="mydata_delete_all",
        )])
        buttons.append([InlineKeyboardButton(
            text=f"← {t('mydata.back_to_hub', lang)}",
            callback_data="mydata_hub",
        )])

        try:
            await callback.message.edit_text(
                text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="Markdown",
            )
        except Exception:
            await self.send(
                user, text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="Markdown",
            )

    # ─── Manage: confirmation step ─────────────────────────────────────

    async def _confirm_action(self, user, callback: CallbackQuery, action: str) -> None:
        """Показать подтверждение перед деструктивным действием."""
        lang = self._get_lang(user)

        confirm_keys = {
            'reset_stats': ('mydata.confirm_reset_stats', 'mydata.confirm_reset_stats_detail'),
            'reset_marathon': ('mydata.confirm_reset_marathon', 'mydata.confirm_reset_marathon_detail'),
            'clear_qa': ('mydata.confirm_clear_qa', 'mydata.confirm_clear_qa_detail'),
            'clear_notes': ('mydata.confirm_clear_notes', 'mydata.confirm_clear_notes_detail'),
            'reset_learning': ('mydata.confirm_reset_learning', 'mydata.confirm_reset_learning_detail'),
        }
        title_key, detail_key = confirm_keys[action]

        text = f"⚠️ *{t(title_key, lang)}*\n\n"
        text += t(detail_key, lang)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ {t('mydata.confirm_yes', lang)}",
                    callback_data=f"mydata_confirm_{action}",
                ),
                InlineKeyboardButton(
                    text=f"❌ {t('mydata.confirm_cancel', lang)}",
                    callback_data="mydata_sec_manage",
                ),
            ],
        ])

        try:
            await callback.message.edit_text(
                text, reply_markup=keyboard, parse_mode="Markdown",
            )
        except Exception:
            await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    # ─── Manage: individual actions ────────────────────────────────────

    async def _reset_stats(self, user) -> None:
        """Сбросить streak и active_days."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        from db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                '''UPDATE development.user_state SET
                    active_days_total = 0, active_days_streak = 0,
                    longest_streak = 0, last_active_date = NULL
                   WHERE chat_id = $1''',
                chat_id,
            )
        await self.send(
            user, f"✅ {t('mydata.stats_reset_done', lang)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"← {t('mydata.back_to_hub', lang)}", callback_data="mydata_hub",
                )],
            ]),
        )

    async def _reset_marathon(self, user) -> None:
        """Сбросить только марафон (ответы + прогресс). Лента и статистика сохраняются."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        from db.queries.answers import delete_marathon_answers
        from db.queries.users import update_intern, derive_mode, moscow_today
        from config import MarathonStatus

        today = moscow_today()
        await delete_marathon_answers(chat_id)

        feed_status = 'not_started'
        if isinstance(user, dict):
            feed_status = user.get('feed_status', 'not_started')
        else:
            feed_status = getattr(user, 'feed_status', 'not_started')

        await update_intern(chat_id,
            completed_topics=[],
            current_topic_index=0,
            marathon_start_date=today,
            marathon_status=MarathonStatus.ACTIVE,
            mode=derive_mode(MarathonStatus.ACTIVE, feed_status),
            topics_today=0,
            topics_at_current_bloom=0,
        )

        await self.send(
            user, f"✅ {t('mydata.marathon_reset_done', lang, date=today.strftime('%d.%m.%Y'))}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"← {t('mydata.back_to_hub', lang)}", callback_data="mydata_hub",
                )],
            ]),
        )

    async def _clear_notes(self, user) -> None:
        """Очистить заметки GitHub (fleeting-notes.md)."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        try:
            from clients.github_api import github_notes
            result = await github_notes.clear_notes(chat_id)
            if result:
                msg = f"✅ {t('mydata.notes_cleared', lang)}"
            else:
                msg = f"❌ {t('mydata.notes_clear_error', lang)}"
        except Exception as e:
            logger.error(f"MyData clear_notes error: {e}")
            msg = f"❌ {t('mydata.notes_clear_error', lang)}"

        await self.send(
            user, msg,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"← {t('mydata.back_to_hub', lang)}", callback_data="mydata_hub",
                )],
            ]),
        )

    async def _clear_qa(self, user) -> None:
        """Очистить историю Q&A."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        from db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                'DELETE FROM qa_history WHERE chat_id = $1', chat_id,
            )
        count = int(result.split()[-1]) if result else 0
        await self.send(
            user, f"✅ {t('mydata.qa_cleared', lang)} ({count})",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"← {t('mydata.back_to_hub', lang)}", callback_data="mydata_hub",
                )],
            ]),
        )

    async def _reset_learning(self, user) -> None:
        """Сбросить весь учебный прогресс (марафон, лента, ответы). Профиль сохраняется."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        from db.queries.profile import reset_learning_data
        result = await reset_learning_data(chat_id)
        total = sum(result.values())
        await self.send(
            user, f"✅ {t('mydata.learning_reset_done', lang)} ({total})",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"← {t('mydata.back_to_hub', lang)}", callback_data="mydata_hub",
                )],
            ]),
        )

    async def _disconnect_github(self, user) -> None:
        """Отключить GitHub OAuth."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        from db.queries.github import delete_github_connection
        await delete_github_connection(chat_id)
        await self.send(
            user, f"✅ {t('mydata.github_disconnected', lang)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"← {t('mydata.back_to_hub', lang)}", callback_data="mydata_hub",
                )],
            ]),
        )

    # ─── Manage: Full delete flow ──────────────────────────────────────

    async def _start_delete_flow(self, user, callback: CallbackQuery) -> None:
        """Начать процедуру полного удаления данных."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        confirm_text = t('mydata.delete_confirm_phrase', lang)

        text = f"⚠️ *{t('mydata.delete_warning_title', lang)}*\n\n"
        text += t('mydata.delete_warning_body', lang) + "\n\n"
        text += f"_{t('mydata.delete_instruction', lang)}_:\n\n"
        text += f"`{confirm_text}`"

        # Сохраняем контекст ожидания подтверждения
        await self._save_context(chat_id, {
            'awaiting_delete': True,
        })

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"← {t('mydata.cancel_delete', lang)}",
                callback_data="mydata_cancel_delete",
            )],
        ])

        try:
            await callback.message.edit_text(
                text, reply_markup=keyboard, parse_mode="Markdown",
            )
        except Exception:
            await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    async def _execute_delete(self, user, chat_id: int, lang: str) -> None:
        """Выполнить каскадное удаление всех данных."""
        from db.queries.profile import delete_all_user_data

        # DT disconnect
        try:
            from clients.digital_twin import digital_twin
            if digital_twin.is_connected(chat_id):
                digital_twin.disconnect(chat_id)
        except Exception:
            pass

        result = await delete_all_user_data(chat_id)
        total = sum(result.values())

        text = f"✅ *{t('mydata.delete_done_title', lang)}*\n\n"
        text += t('mydata.delete_done_body', lang, count=total) + "\n"
        text += t('mydata.delete_done_restart', lang)

        await self.send(user, text, parse_mode="Markdown")

    # ─── Context persistence (via current_context in user_state) ────────
    # NB: fsm_states.data затирается fallback state.clear() при каждом
    # текстовом сообщении → хранить в current_context (user_state).

    async def _get_context(self, chat_id: int) -> Optional[dict]:
        from db.queries import get_intern
        intern = await get_intern(chat_id)
        if not intern:
            return None
        ctx = intern.get('current_context', {})
        return ctx.get('mydata_context')

    async def _save_context(self, chat_id: int, ctx: dict) -> None:
        from db.queries import get_intern
        from db.queries.users import update_intern
        intern = await get_intern(chat_id)
        current_ctx = intern.get('current_context', {}) if intern else {}
        current_ctx['mydata_context'] = ctx
        await update_intern(chat_id, current_context=current_ctx)

    async def _clear_context(self, chat_id: int) -> None:
        await self._save_context(chat_id, {})
