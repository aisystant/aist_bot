"""
Стейт: Мои данные (/mydata).

Показывает пользователю все его данные из БД (категоризированно)
и объясняет почему данные такие через Claude L2.

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

# Категории данных: ключ → поля из user_knowledge_profile VIEW
CATEGORIES = {
    'profile': [
        'name', 'occupation', 'role', 'domain',
        'interests', 'goals', 'motivation', 'language', 'experience_level',
    ],
    'learning': [
        'mode', 'marathon_status', 'feed_status',
        'current_topic_index', 'complexity_level',
        'assessment_state', 'assessment_date',
    ],
    'activity': [
        'active_days_total', 'active_days_streak', 'longest_streak',
        'last_active_date',
    ],
    'marathon': [
        'theory_answers_count', 'work_products_count', 'qa_count',
    ],
    'feed': [
        'total_digests', 'total_fixations', 'current_feed_topics',
    ],
}

# Человекочитаемые названия полей
FIELD_LABELS = {
    'ru': {
        'name': 'Имя', 'occupation': 'Профессия', 'role': 'Роль',
        'domain': 'Домен', 'interests': 'Интересы', 'goals': 'Цели',
        'motivation': 'Мотивация', 'language': 'Язык',
        'experience_level': 'Уровень опыта',
        'mode': 'Режим', 'marathon_status': 'Статус марафона',
        'feed_status': 'Статус ленты',
        'current_topic_index': 'Текущая тема (индекс)',
        'complexity_level': 'Уровень сложности',
        'assessment_state': 'Результат теста',
        'assessment_date': 'Дата теста',
        'active_days_total': 'Всего активных дней',
        'active_days_streak': 'Текущая серия',
        'longest_streak': 'Максимальная серия',
        'last_active_date': 'Последняя активность',
        'theory_answers_count': 'Ответов на теорию',
        'work_products_count': 'Рабочих продуктов',
        'qa_count': 'Вопросов консультанту',
        'total_digests': 'Дайджестов',
        'total_fixations': 'Фиксаций',
        'current_feed_topics': 'Текущие темы',
    },
    'en': {
        'name': 'Name', 'occupation': 'Occupation', 'role': 'Role',
        'domain': 'Domain', 'interests': 'Interests', 'goals': 'Goals',
        'motivation': 'Motivation', 'language': 'Language',
        'experience_level': 'Experience level',
        'mode': 'Mode', 'marathon_status': 'Marathon status',
        'feed_status': 'Feed status',
        'current_topic_index': 'Current topic (index)',
        'complexity_level': 'Complexity level',
        'assessment_state': 'Assessment result',
        'assessment_date': 'Assessment date',
        'active_days_total': 'Total active days',
        'active_days_streak': 'Current streak',
        'longest_streak': 'Longest streak',
        'last_active_date': 'Last active date',
        'theory_answers_count': 'Theory answers',
        'work_products_count': 'Work products',
        'qa_count': 'Consultant questions',
        'total_digests': 'Digests',
        'total_fixations': 'Fixations',
        'current_feed_topics': 'Current topics',
    },
    'es': {
        'name': 'Nombre', 'occupation': 'Profesión', 'role': 'Rol',
        'domain': 'Dominio', 'interests': 'Intereses', 'goals': 'Objetivos',
        'motivation': 'Motivación', 'language': 'Idioma',
        'experience_level': 'Nivel de experiencia',
        'mode': 'Modo', 'marathon_status': 'Estado del maratón',
        'feed_status': 'Estado del feed',
        'current_topic_index': 'Tema actual (índice)',
        'complexity_level': 'Nivel de complejidad',
        'assessment_state': 'Resultado del test',
        'assessment_date': 'Fecha del test',
        'active_days_total': 'Días activos totales',
        'active_days_streak': 'Racha actual',
        'longest_streak': 'Racha más larga',
        'last_active_date': 'Última actividad',
        'theory_answers_count': 'Respuestas de teoría',
        'work_products_count': 'Productos de trabajo',
        'qa_count': 'Preguntas al consultor',
        'total_digests': 'Resúmenes',
        'total_fixations': 'Fijaciones',
        'current_feed_topics': 'Temas actuales',
    },
    'fr': {
        'name': 'Nom', 'occupation': 'Profession', 'role': 'Rôle',
        'domain': 'Domaine', 'interests': 'Intérêts', 'goals': 'Objectifs',
        'motivation': 'Motivation', 'language': 'Langue',
        'experience_level': "Niveau d'expérience",
        'mode': 'Mode', 'marathon_status': 'Statut du marathon',
        'feed_status': 'Statut du feed',
        'current_topic_index': 'Sujet actuel (index)',
        'complexity_level': 'Niveau de complexité',
        'assessment_state': 'Résultat du test',
        'assessment_date': 'Date du test',
        'active_days_total': 'Jours actifs totaux',
        'active_days_streak': 'Série actuelle',
        'longest_streak': 'Plus longue série',
        'last_active_date': 'Dernière activité',
        'theory_answers_count': 'Réponses théoriques',
        'work_products_count': 'Produits de travail',
        'qa_count': 'Questions au consultant',
        'total_digests': 'Résumés',
        'total_fixations': 'Fixations',
        'current_feed_topics': 'Sujets actuels',
    },
    'zh': {
        'name': '姓名', 'occupation': '职业', 'role': '角色',
        'domain': '领域', 'interests': '兴趣', 'goals': '目标',
        'motivation': '动机', 'language': '语言',
        'experience_level': '经验水平',
        'mode': '模式', 'marathon_status': '马拉松状态',
        'feed_status': '信息流状态',
        'current_topic_index': '当前主题（索引）',
        'complexity_level': '难度级别',
        'assessment_state': '测试结果',
        'assessment_date': '测试日期',
        'active_days_total': '总活跃天数',
        'active_days_streak': '当前连续天数',
        'longest_streak': '最长连续天数',
        'last_active_date': '最后活跃日期',
        'theory_answers_count': '理论回答数',
        'work_products_count': '工作成果数',
        'qa_count': '咨询问题数',
        'total_digests': '摘要数',
        'total_fixations': '固定数',
        'current_feed_topics': '当前主题',
    },
}


class MyDataState(BaseState):
    """
    Стейт просмотра и объяснения данных пользователя.

    enter() → сводка по категориям
    handle_callback("mydata_cat_*") → детали категории
    handle_callback("mydata_why_*") → Claude объясняет данные
    """

    name = "utility.mydata"
    display_name = {
        "ru": "Мои данные",
        "en": "My Data",
        "es": "Mis datos",
        "fr": "Mes données",
        "zh": "我的数据"
    }
    allow_global = ["consultation", "notes"]

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
        """Человекочитаемое название поля."""
        labels = FIELD_LABELS.get(lang, FIELD_LABELS['en'])
        return labels.get(field, field)

    def _format_value(self, value) -> str:
        """Форматировать значение для отображения."""
        if value is None:
            return "—"
        if isinstance(value, list):
            return ", ".join(str(v) for v in value) if value else "—"
        if hasattr(value, 'strftime'):
            return value.strftime('%d.%m.%Y')
        return str(value)

    async def _get_profile(self, chat_id: int) -> Optional[dict]:
        from db.queries.profile import get_knowledge_profile
        return await get_knowledge_profile(chat_id)

    async def enter(self, user, context: dict = None) -> None:
        """Показать сводку данных по категориям."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)

        profile = await self._get_profile(chat_id)
        if not profile:
            await self.send(user, t('mydata.no_data', lang))
            return

        text = f"*{t('mydata.title', lang)}*\n{t('mydata.summary', lang)}\n\n"

        # Краткая сводка по категориям
        text += f"👤 {t('mydata.cat_profile', lang)}: {profile.get('name', '—')}, {profile.get('occupation', '—')}\n"

        mode = profile.get('mode', '—')
        complexity = profile.get('complexity_level', 1)
        text += f"📚 {t('mydata.cat_learning', lang)}: {mode}, {t('mydata.complexity', lang)} {complexity}\n"

        streak = profile.get('active_days_streak', 0)
        total = profile.get('active_days_total', 0)
        text += f"🔥 {t('mydata.cat_activity', lang)}: {streak} {t('mydata.streak', lang)}, {total} {t('mydata.total', lang)}\n"

        wp = profile.get('work_products_count', 0)
        theory = profile.get('theory_answers_count', 0)
        text += f"🏃 {t('mydata.cat_marathon', lang)}: {wp} {t('mydata.wp', lang)}, {theory} {t('mydata.answers', lang)}\n"

        digests = profile.get('total_digests', 0)
        fixations = profile.get('total_fixations', 0)
        text += f"📖 {t('mydata.cat_feed', lang)}: {digests} {t('mydata.digests', lang)}, {fixations} {t('mydata.fixations', lang)}"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=f"👤 {t('mydata.cat_profile', lang)}", callback_data="mydata_cat_profile"),
                InlineKeyboardButton(text=f"📚 {t('mydata.cat_learning', lang)}", callback_data="mydata_cat_learning"),
            ],
            [
                InlineKeyboardButton(text=f"🔥 {t('mydata.cat_activity', lang)}", callback_data="mydata_cat_activity"),
                InlineKeyboardButton(text=f"⚙️ {t('buttons.settings', lang)}", callback_data="mydata_settings"),
            ],
            [
                InlineKeyboardButton(text=f"← {t('buttons.back', lang)}", callback_data="mydata_back"),
            ],
        ])

        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    async def handle(self, user, message: Message) -> Optional[str]:
        """Текстовый ввод не обрабатывается."""
        return None

    async def handle_callback(self, user, callback: CallbackQuery) -> Optional[str]:
        """Обработка inline-кнопок."""
        data = callback.data
        await callback.answer()

        if data == "mydata_back":
            return "back"

        if data == "mydata_settings":
            return "settings"

        if data == "mydata_overview":
            await self.enter(user)
            return None

        # Категория: mydata_cat_<category>
        if data.startswith("mydata_cat_"):
            category = data.replace("mydata_cat_", "")
            if category in CATEGORIES:
                await self._show_category(user, category)
            return None

        # Почему: mydata_why_<category>
        if data.startswith("mydata_why_"):
            category = data.replace("mydata_why_", "")
            if category in CATEGORIES:
                await self._explain_category(user, category)
            return None

        return None

    async def _show_category(self, user, category: str) -> None:
        """Показать детали одной категории."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        profile = await self._get_profile(chat_id)

        if not profile:
            await self.send(user, t('mydata.no_data', lang))
            return

        cat_label = t(f'mydata.cat_{category}', lang)
        fields = CATEGORIES[category]

        text = f"*{cat_label}*\n\n"
        for field in fields:
            label = self._field_label(field, lang)
            value = self._format_value(profile.get(field))
            text += f"• {label}: {value}\n"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"❓ {t('mydata.why_button', lang)}",
                callback_data=f"mydata_why_{category}",
            )],
            [InlineKeyboardButton(
                text=f"← {t('mydata.back_to_overview', lang)}",
                callback_data="mydata_overview",
            )],
        ])

        await self.send(user, text, reply_markup=keyboard, parse_mode="Markdown")

    async def _explain_category(self, user, category: str) -> None:
        """Claude L2 объясняет почему данные такие."""
        lang = self._get_lang(user)
        chat_id = self._get_chat_id(user)
        profile = await self._get_profile(chat_id)

        if not profile:
            await self.send(user, t('mydata.no_data', lang))
            return

        # Показать "Анализирую..."
        await self.send(user, f"⏳ {t('mydata.why_thinking', lang)}")

        # Собрать данные категории
        fields = CATEGORIES[category]
        cat_data = {f: self._format_value(profile.get(f)) for f in fields}
        cat_label = t(f'mydata.cat_{category}', lang)

        lang_instruction = {
            'ru': "Отвечай на русском. Будь дружелюбным и конкретным.",
            'en': "Answer in English. Be friendly and specific.",
            'es': "Responde en español. Sé amigable y específico.",
            'fr': "Réponds en français. Sois amical et précis.",
            'zh': "请用中文回答。保持友好和具体。",
        }.get(lang, "Answer in English.")

        system_prompt = f"""Ты — AIST Bot, бот-наставник для систематического обучения.
Пользователь смотрит свои данные в категории «{cat_label}» и хочет понять, ПОЧЕМУ они именно такие.

{lang_instruction}

ДАННЫЕ ПОЛЬЗОВАТЕЛЯ (категория «{cat_label}»):
{json.dumps(cat_data, ensure_ascii=False, indent=2)}

ПОЛНЫЙ КОНТЕКСТ (все категории):
{json.dumps({f: self._format_value(profile.get(f)) for f in sum(CATEGORIES.values(), []) if profile.get(f) is not None}, ensure_ascii=False, indent=2)}

ИНСТРУКЦИИ:
1. Объясни в 3-5 предложениях, откуда берутся эти данные
2. Свяжи числа с действиями пользователя (пример: «4 рабочих продукта = 3 дня марафона + 1 дополнительный РП за день 3»)
3. Если данные пустые — объясни, что нужно сделать чтобы они появились
4. НЕ придумывай данные, которых нет в контексте"""

        from clients import claude
        try:
            answer = await claude.generate(
                system_prompt=system_prompt,
                user_prompt=f"Объясни мои данные в категории «{cat_label}»",
            )
        except Exception as e:
            logger.error(f"MyData explain error: {e}")
            answer = None

        if not answer:
            answer = t('mydata.explain_error', lang) if t('mydata.explain_error', lang) != 'mydata.explain_error' else "—"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"← {t('mydata.back_to_overview', lang)}",
                callback_data="mydata_overview",
            )],
        ])

        try:
            await self.send(user, answer, reply_markup=keyboard, parse_mode="Markdown")
        except Exception:
            await self.send(user, answer, reply_markup=keyboard)
