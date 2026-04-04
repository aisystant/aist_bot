"""
Клавиатуры для Telegram бота AIST_me_bot

Вынесено из bot.py для улучшения структуры кода.
"""

from datetime import timedelta

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from i18n import t, get_language_name, SUPPORTED_LANGUAGES


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
    emojis = {'easy': '🔰', 'medium': '🌿', 'hard': '🌳'}
    keys = ['easy', 'medium', 'hard']
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{emojis[k]} {t(f'difficulty.{k}', lang)}", callback_data=f"diff_{k}")]
        for k in keys
    ])


def kb_learning_style(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура выбора стиля обучения"""
    emojis = {'theoretical': '📐', 'practical': '🔧', 'mixed': '⚖️'}
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
            InlineKeyboardButton(text="🔁", callback_data="restart")
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


def kb_slot_suggestions(target_time: str, slots: dict[str, int], lang: str = 'ru') -> InlineKeyboardMarkup:
    """Кнопки с ближайшими свободными слотами при перегрузке.

    Args:
        target_time: запрошенное время (HH:MM)
        slots: dict {time_str: user_count} — нагрузка на слотах вокруг target_time
        lang: язык пользователя
    """
    from db.queries.users import MAX_USERS_PER_SLOT

    # Сортируем по расстоянию от целевого, потом по загрузке
    h, m = map(int, target_time.split(":"))
    target_total = h * 60 + m

    def sort_key(slot: str) -> tuple:
        sh, sm = map(int, slot.split(":"))
        dist = abs((sh * 60 + sm) - target_total)
        if dist > 720:
            dist = 1440 - dist
        return (dist, slots[slot])

    available = [s for s in sorted(slots.keys(), key=sort_key)
                 if slots[s] < MAX_USERS_PER_SLOT and s != target_time][:3]

    buttons = []
    for slot in available:
        count = slots[slot]
        buttons.append([InlineKeyboardButton(
            text=f"🟢 {slot} ({count} чел.)",
            callback_data=f"slot_{slot}"
        )])

    # Кнопка «оставить как есть»
    keep_labels = {
        'ru': 'Оставить', 'en': 'Keep', 'es': 'Mantener',
        'fr': 'Garder', 'zh': '保留',
    }
    keep = keep_labels.get(lang, 'Keep')
    target_count = slots.get(target_time, 0)
    buttons.append([InlineKeyboardButton(
        text=f"🟡 {keep} {target_time} ({target_count} чел.)",
        callback_data=f"slot_{target_time}"
    )])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_marathon_start(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура для выбора даты старта марафона"""
    from db.queries.users import moscow_today
    today = moscow_today()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    # Названия дней на разных языках
    day_names = {
        'ru': ('Сегодня', 'Завтра', 'Послезавтра'),
        'en': ('Today', 'Tomorrow', 'Day after'),
        'es': ('Hoy', 'Mañana', 'Pasado mañana'),
        'fr': ('Aujourd\'hui', 'Demain', 'Après-demain'),
        'zh': ('今天', '明天', '后天')
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
         InlineKeyboardButton(text="🏢 " + t('buttons.occupation', lang), callback_data="upd_occupation")],
        [InlineKeyboardButton(text="🎨 " + t('buttons.interests', lang), callback_data="upd_interests"),
         InlineKeyboardButton(text="🎯 " + t('buttons.goals', lang), callback_data="upd_goals")],
        [InlineKeyboardButton(text="⏱ " + t('buttons.duration', lang), callback_data="upd_duration"),
         InlineKeyboardButton(text="⏰ " + t('buttons.schedule', lang), callback_data="upd_schedule")],
        [InlineKeyboardButton(text="📊 " + t('buttons.difficulty', lang), callback_data="upd_bloom"),
         InlineKeyboardButton(text="📚 " + t('buttons.delivery_style', lang), callback_data="upd_delivery")],
        [InlineKeyboardButton(text="🤖 " + t('buttons.bot_mode', lang), callback_data="upd_mode"),
         InlineKeyboardButton(text="🌐 " + t('buttons.change_language', lang), callback_data="upd_language")],
        [InlineKeyboardButton(text="🏛 Клуб", callback_data="upd_club")],
        [InlineKeyboardButton(text=t('buttons.reset_marathon', lang), callback_data="marathon_reset_confirm"),
         InlineKeyboardButton(text=t('progress.reset_stats_btn', lang), callback_data="stats_reset_confirm")],
    ])


def kb_delivery_format(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура выбора формата подачи (WP-151 Ф2)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('delivery.examples_btn', lang), callback_data="delf_examples")],
        [InlineKeyboardButton(text=t('delivery.tasks_btn', lang), callback_data="delf_tasks")],
        [InlineKeyboardButton(text=t('delivery.mix_btn', lang), callback_data="delf_mix")],
    ])


def kb_detail_level(lang: str = 'ru') -> InlineKeyboardMarkup:
    """Клавиатура выбора уровня детализации (WP-151 Ф2)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t('delivery.brief_btn', lang), callback_data="detl_brief")],
        [InlineKeyboardButton(text=t('delivery.detailed_btn', lang), callback_data="detl_detailed")],
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
