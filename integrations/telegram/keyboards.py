"""
Клавиатуры для Telegram бота AIST Track

Вынесено из bot.py для улучшения структуры кода.
"""

from datetime import timedelta

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from i18n import t, get_language_name, SUPPORTED_LANGUAGES


def moscow_today():
    """Получить текущую дату по Москве"""
    from datetime import datetime, timezone
    MOSCOW_TZ = timezone(timedelta(hours=3))
    return datetime.now(MOSCOW_TZ).date()


# ============= КЛАВИАТУРЫ ОНБОРДИНГА =============

def kb_experience(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура выбора уровня опыта"""
    emojis = {'student': '🎓', 'junior': '🌱', 'middle': '💼', 'senior': '⭐', 'switching': '🔄'}
    keys = ['student', 'junior', 'middle', 'senior', 'switching']
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{emojis[k]} {t(f'experience.{k}', lang)}", callback_data=f"exp_{k}")]
        for k in keys
    ])


def kb_difficulty(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура выбора сложности"""
    emojis = {'easy': '🌱', 'medium': '🌿', 'hard': '🌳'}
    keys = ['easy', 'medium', 'hard']
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{emojis[k]} {t(f'difficulty.{k}', lang)}", callback_data=f"diff_{k}")]
        for k in keys
    ])


def kb_learning_style(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура выбора стиля обучения"""
    emojis = {'theoretical': '📚', 'practical': '🔧', 'mixed': '⚖️'}
    keys = ['theoretical', 'practical', 'mixed']
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{emojis[k]} {t(f'learning_style.{k}', lang)}", callback_data=f"style_{k}")]
        for k in keys
    ])


def kb_study_duration(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура выбора длительности занятия"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(f'duration.minutes_{k}', lang), callback_data=f"duration_{k}")]
        for k in [5, 15, 25]
    ])


def kb_confirm(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура подтверждения"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t('buttons.yes', lang), callback_data="confirm"),
            InlineKeyboardButton(text="🔄", callback_data="restart")
        ]
    ])


# ============= КЛАВИАТУРЫ ОБУЧЕНИЯ =============

def kb_learn(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура начала обучения"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('buttons.start_now', lang), callback_data="learn")],
        [InlineKeyboardButton(text=t('buttons.start_scheduled', lang), callback_data="later")]
    ])


def kb_bloom_level(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура для выбора уровня сложности (Bloom)"""
    emojis = {1: '🔵', 2: '🟡', 3: '🔴'}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{emojis[k]} {t(f'bloom.level_{k}_short', lang)} — {t(f'bloom.level_{k}_desc', lang)}",
            callback_data=f"bloom_{k}"
        )]
        for k in [1, 2, 3]
    ])


def kb_bonus_question(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура для предложения дополнительного вопроса"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('buttons.bonus_yes', lang), callback_data="bonus_yes")],
        [InlineKeyboardButton(text=t('buttons.bonus_no', lang), callback_data="bonus_no")]
    ])


def kb_skip_topic(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой пропуска темы"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('buttons.skip_topic', lang), callback_data="skip_topic")]
    ])


def kb_marathon_start(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура для выбора даты старта марафона"""
    today = moscow_today()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    # Названия дней на разных языках
    day_names = {
        'ru': ('Сегодня', 'Завтра', 'Послезавтра'),
        'en': ('Today', 'Tomorrow', 'Day after'),
        'es': ('Hoy', 'Mañana', 'Pasado mañana'),
        'fr': ('Aujourd\'hui', 'Demain', 'Après-demain')
    }
    names = day_names.get(lang, day_names['en'])

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🚀 {names[0]}", callback_data="start_today")],
        [InlineKeyboardButton(text=f"📅 {names[1]} ({tomorrow.strftime('%d.%m')})", callback_data="start_tomorrow")],
        [InlineKeyboardButton(text=f"📅 {names[2]} ({day_after.strftime('%d.%m')})", callback_data="start_day_after")]
    ])


def kb_submit_work_product(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура для практического задания"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('buttons.skip_practice', lang), callback_data="skip_practice")]
    ])


# ============= КЛАВИАТУРЫ ПРОФИЛЯ =============

def kb_update_profile(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура редактирования профиля"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 " + t('buttons.name', lang), callback_data="upd_name"),
         InlineKeyboardButton(text="💼 " + t('buttons.occupation', lang), callback_data="upd_occupation")],
        [InlineKeyboardButton(text="🎨 " + t('buttons.interests', lang), callback_data="upd_interests"),
         InlineKeyboardButton(text="🎯 " + t('buttons.goals', lang), callback_data="upd_goals")],
        [InlineKeyboardButton(text="⏱ " + t('buttons.duration', lang), callback_data="upd_duration"),
         InlineKeyboardButton(text="⏰ " + t('buttons.schedule', lang), callback_data="upd_schedule")],
        [InlineKeyboardButton(text="📊 " + t('buttons.difficulty', lang), callback_data="upd_bloom"),
         InlineKeyboardButton(text="🤖 " + t('buttons.bot_mode', lang), callback_data="upd_mode")],
        [InlineKeyboardButton(text="🌐 Language (en, es, fr, ru)", callback_data="upd_language")]
    ])


def kb_language_select() -> InlineKeyboardMarkup:
    """Клавиатура для выбора языка интерфейса"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_language_name(lang), callback_data=f"lang_{lang}")]
        for lang in SUPPORTED_LANGUAGES
    ])


# ============= УТИЛИТЫ =============

def progress_bar(completed: int, total: int) -> str:
    """Визуальный прогресс-бар"""
    pct = int((completed / total) * 100) if total > 0 else 0
    # Показываем хотя бы 1 заполненный кубик, если есть прогресс
    filled = max(1, pct // 10) if pct > 0 else 0
    return f"{'█' * filled}{'░' * (10 - filled)} {pct}%"
