"""
Модуль локализации для бота AI System Track

Поддерживаемые языки: RU, EN, ES
"""

from typing import Optional

SUPPORTED_LANGUAGES = ['ru', 'en', 'es']

# Переводы
TRANSLATIONS = {
    'ru': {
        # Приветствие
        'welcome.greeting': 'Привет!',
        'welcome.intro': 'Я помогу тебе стать систематическим учеником.',
        'welcome.ask_name': 'Как тебя зовут?',
        'welcome.returning': 'С возвращением, {name}!',

        # Команды
        'commands.learn': '/learn — получить тему',
        'commands.progress': '/progress — мой прогресс',
        'commands.profile': '/profile — мой профиль',
        'commands.update': '/update — обновить настройки',

        # Онбординг
        'onboarding.nice_to_meet': 'Приятно познакомиться, {name}!',
        'onboarding.ask_name': 'Как тебя зовут?',
        'onboarding.ask_occupation': 'Чем ты занимаешься?',
        'onboarding.ask_occupation_hint': '_Например: разработчик, маркетолог, студент_',
        'onboarding.ask_interests': 'Какие у тебя интересы и хобби?',
        'onboarding.ask_interests_hint': '_Через запятую: гольф, чтение, путешествия_',
        'onboarding.ask_interests_why': '_Это поможет приводить близкие тебе примеры._',
        'onboarding.ask_values': 'Что для тебя по-настоящему важно в жизни?',
        'onboarding.ask_values_hint': '_Это поможет добавлять мотивационные блоки._',
        'onboarding.ask_goals': 'Что ты хочешь изменить в своей жизни?',
        'onboarding.ask_goals_hint': '_Это поможет связать материал с твоими целями._',
        'onboarding.ask_duration': 'Сколько минут готов уделять одной теме?',
        'onboarding.ask_time': 'Во сколько напоминать о новой теме?',
        'onboarding.ask_time_hint': '_Формат: ЧЧ:ММ (например 09:00). Часовой пояс: Москва (UTC+3)_',
        'onboarding.ask_start_date': 'Когда начнём марафон?',

        # Кнопки
        'buttons.yes': 'Да',
        'buttons.cancel': 'Отмена',
        'buttons.start_now': 'Начать сейчас',
        'buttons.start_scheduled': 'По расписанию',
        'buttons.change_language': 'Сменить язык',

        # Настройки
        'settings.title': 'Настройки',
        'settings.what_to_change': 'Что хочешь обновить?',
        'settings.language.title': 'Выбери язык:',
        'settings.language.changed': 'Язык изменён на русский!',

        # Прогресс
        'progress.day': 'День {day} из {total}',

        # Режимы
        'modes.select': 'Выбери режим',
        'modes.marathon_desc': '14-дневный марафон',

        # Ошибки
        'errors.try_again': 'Попробуй ещё раз',
    },

    'en': {
        # Welcome
        'welcome.greeting': 'Hello!',
        'welcome.intro': "I'll help you become a systematic learner.",
        'welcome.ask_name': "What's your name?",
        'welcome.returning': 'Welcome back, {name}!',

        # Commands
        'commands.learn': '/learn — get a topic',
        'commands.progress': '/progress — my progress',
        'commands.profile': '/profile — my profile',
        'commands.update': '/update — update settings',

        # Onboarding
        'onboarding.nice_to_meet': 'Nice to meet you, {name}!',
        'onboarding.ask_name': "What's your name?",
        'onboarding.ask_occupation': 'What do you do?',
        'onboarding.ask_occupation_hint': '_For example: developer, marketer, student_',
        'onboarding.ask_interests': 'What are your interests and hobbies?',
        'onboarding.ask_interests_hint': '_Comma-separated: golf, reading, travel_',
        'onboarding.ask_interests_why': "_This helps me give relevant examples._",
        'onboarding.ask_values': "What's truly important to you in life?",
        'onboarding.ask_values_hint': "_This helps add motivational blocks._",
        'onboarding.ask_goals': 'What do you want to change in your life?',
        'onboarding.ask_goals_hint': "_This helps connect material with your goals._",
        'onboarding.ask_duration': 'How many minutes per topic?',
        'onboarding.ask_time': 'When should I remind you about new topics?',
        'onboarding.ask_time_hint': '_Format: HH:MM (e.g. 09:00). Timezone: Moscow (UTC+3)_',
        'onboarding.ask_start_date': 'When shall we start the marathon?',

        # Buttons
        'buttons.yes': 'Yes',
        'buttons.cancel': 'Cancel',
        'buttons.start_now': 'Start now',
        'buttons.start_scheduled': 'Scheduled',
        'buttons.change_language': 'Change language',

        # Settings
        'settings.title': 'Settings',
        'settings.what_to_change': 'What would you like to update?',
        'settings.language.title': 'Choose language:',
        'settings.language.changed': 'Language changed to English!',

        # Progress
        'progress.day': 'Day {day} of {total}',

        # Modes
        'modes.select': 'Select mode',
        'modes.marathon_desc': '14-day marathon',

        # Errors
        'errors.try_again': 'Try again',
    },

    'es': {
        # Bienvenida
        'welcome.greeting': '¡Hola!',
        'welcome.intro': 'Te ayudaré a convertirte en un estudiante sistemático.',
        'welcome.ask_name': '¿Cómo te llamas?',
        'welcome.returning': '¡Bienvenido de nuevo, {name}!',

        # Comandos
        'commands.learn': '/learn — obtener tema',
        'commands.progress': '/progress — mi progreso',
        'commands.profile': '/profile — mi perfil',
        'commands.update': '/update — actualizar ajustes',

        # Onboarding
        'onboarding.nice_to_meet': '¡Mucho gusto, {name}!',
        'onboarding.ask_name': '¿Cómo te llamas?',
        'onboarding.ask_occupation': '¿A qué te dedicas?',
        'onboarding.ask_occupation_hint': '_Por ejemplo: desarrollador, marketing, estudiante_',
        'onboarding.ask_interests': '¿Cuáles son tus intereses y hobbies?',
        'onboarding.ask_interests_hint': '_Separados por comas: golf, lectura, viajes_',
        'onboarding.ask_interests_why': '_Esto me ayuda a dar ejemplos relevantes._',
        'onboarding.ask_values': '¿Qué es verdaderamente importante para ti?',
        'onboarding.ask_values_hint': '_Esto ayuda a añadir bloques motivacionales._',
        'onboarding.ask_goals': '¿Qué quieres cambiar en tu vida?',
        'onboarding.ask_goals_hint': '_Esto ayuda a conectar el material con tus metas._',
        'onboarding.ask_duration': '¿Cuántos minutos por tema?',
        'onboarding.ask_time': '¿Cuándo debo recordarte sobre nuevos temas?',
        'onboarding.ask_time_hint': '_Formato: HH:MM (ej. 09:00). Zona horaria: Moscú (UTC+3)_',
        'onboarding.ask_start_date': '¿Cuándo empezamos el maratón?',

        # Botones
        'buttons.yes': 'Sí',
        'buttons.cancel': 'Cancelar',
        'buttons.start_now': 'Empezar ahora',
        'buttons.start_scheduled': 'Programado',
        'buttons.change_language': 'Cambiar idioma',

        # Ajustes
        'settings.title': 'Ajustes',
        'settings.what_to_change': '¿Qué quieres actualizar?',
        'settings.language.title': 'Elige idioma:',
        'settings.language.changed': '¡Idioma cambiado a español!',

        # Progreso
        'progress.day': 'Día {day} de {total}',

        # Modos
        'modes.select': 'Seleccionar modo',
        'modes.marathon_desc': 'Maratón de 14 días',

        # Errores
        'errors.try_again': 'Inténtalo de nuevo',
    }
}

# Названия языков
LANGUAGE_NAMES = {
    'ru': 'Русский',
    'en': 'English',
    'es': 'Español'
}


def detect_language(language_code: Optional[str]) -> str:
    """Определяет язык по коду из Telegram"""
    if not language_code:
        return 'ru'

    code = language_code.lower()[:2]

    if code in SUPPORTED_LANGUAGES:
        return code

    # Маппинг похожих языков
    mapping = {
        'uk': 'ru',  # Украинский → Русский
        'be': 'ru',  # Белорусский → Русский
        'kk': 'ru',  # Казахский → Русский
        'pt': 'es',  # Португальский → Испанский
    }

    return mapping.get(code, 'en')  # По умолчанию английский


def get_language_name(lang: str) -> str:
    """Возвращает название языка"""
    return LANGUAGE_NAMES.get(lang, lang)


def t(key: str, lang: str = 'ru', **kwargs) -> str:
    """
    Получить перевод по ключу

    Args:
        key: ключ перевода (например 'welcome.greeting')
        lang: код языка ('ru', 'en', 'es')
        **kwargs: параметры для форматирования (например name='Иван')

    Returns:
        Переведённая строка или ключ если перевод не найден
    """
    # Получаем словарь переводов для языка
    translations = TRANSLATIONS.get(lang, TRANSLATIONS['ru'])

    # Получаем перевод
    text = translations.get(key)

    # Если не найден — пробуем русский
    if text is None:
        text = TRANSLATIONS['ru'].get(key)

    # Если всё ещё не найден — возвращаем ключ
    if text is None:
        return key

    # Форматируем с параметрами
    if kwargs:
        try:
            text = text.format(**kwargs)
        except KeyError:
            pass

    return text
