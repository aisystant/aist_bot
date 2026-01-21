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
        'commands.language': '/language — сменить язык',
        'commands.help': '/help — справка',

        # Описания команд для меню
        'menu.learn': 'Получить новую тему',
        'menu.progress': 'Мой прогресс',
        'menu.profile': 'Мой профиль',
        'menu.update': 'Обновить профиль',
        'menu.mode': 'Выбор режима',
        'menu.language': 'Сменить язык',
        'menu.start': 'Перезапустить онбординг',
        'menu.help': 'Справка',

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

        # Длительность
        'duration.minutes_5': '⚡ 5 минут',
        'duration.minutes_15': '🕑 15 минут',
        'duration.minutes_25': '🕓 25 минут',

        # Кнопки
        'buttons.yes': 'Да',
        'buttons.cancel': 'Отмена',
        'buttons.start_now': 'Начать сейчас',
        'buttons.start_scheduled': 'По расписанию',
        'buttons.change_language': 'Сменить язык',

        # Кнопки профиля
        'buttons.name': 'Имя',
        'buttons.occupation': 'Занятие',
        'buttons.interests': 'Интересы',
        'buttons.values': 'Ценности',
        'buttons.goals': 'Цели',
        'buttons.duration': 'Время',
        'buttons.schedule': 'Расписание',
        'buttons.difficulty': 'Сложность',
        'buttons.bot_mode': 'Режим бота',

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

        # Профиль
        'profile.what_important': 'Что важно',
        'profile.what_change': 'Что изменить',
        'profile.reminder_at': 'Напоминание в',
        'profile.not_specified': 'не указано',
        'profile.first_start': 'Сначала /start',

        # Обновление профиля
        'update.your_name': 'Ваше имя',
        'update.whats_your_name': 'Как вас зовут?',
        'update.your_occupation': 'Ваше занятие',
        'update.whats_your_occupation': 'Чем вы занимаетесь?',
        'update.your_interests': 'Ваши интересы',
        'update.what_interests': 'Какие у вас интересы?',
        'update.your_goals': 'Ваши цели',
        'update.what_goals': 'Что хотите изменить в жизни?',
        'update.current_time': 'Текущее время',
        'update.how_many_minutes': 'Сколько минут готовы уделять изучению одной темы?',
        'update.current_schedule': 'Текущее расписание',
        'update.when_remind': 'Во сколько напоминать о новой теме? (ЧЧ:ММ)',
        'update.current_difficulty': 'Текущий уровень сложности',
        'update.select_difficulty': 'Выберите уровень сложности:',
        'update.saved': 'Сохранено!',
        'update.name_changed': 'Имя изменено',
        'update.occupation_changed': 'Занятие изменено',
        'update.interests_changed': 'Интересы обновлены',
        'update.goals_changed': 'Цели обновлены',
        'update.duration_changed': 'Время на тему изменено',
        'update.schedule_changed': 'Расписание изменено',
        'update.difficulty_changed': 'Сложность изменена',

        # Справка
        'help.title': 'Основные команды',
        'help.how_it_works': 'Как работает обучение',
        'help.step1': '1. Я отправляю персонализированный материал',
        'help.step2': '2. Вы изучаете его (5-25 мин)',
        'help.step3': '3. Отвечаете на вопрос для закрепления',
        'help.step4': '4. Тема засчитывается в прогресс',
        'help.schedule_note': 'Материал буду отправлять в заданное время или по /learn',
        'help.feedback': 'Замечания и предложения',

        # Ошибки
        'errors.try_again': 'Попробуй ещё раз',

        # Загрузка
        'loading.generating_topics': '⏳ Генерирую темы...',
        'loading.generating_content': '⏳ Готовлю материал...',
        'loading.processing': '⏳ Обрабатываю...',

        # Лента (Feed)
        'feed.suggested_topics': 'Предлагаемые темы',
        'feed.select_hint': 'Выберите кнопками или напишите текстом:',
        'feed.select_example': 'Например: «1, 3» или «тема 2 и ещё хочу про собранность»',
        'feed.topics_selected': 'Темы выбраны!',
        'feed.topics_count': 'Выбрано {count} тем.',
        'feed.selected_topics': 'Выбранные темы:',
        'feed.your_topics': 'ваши темы',
        'feed.what_next': 'Когда хочешь начать?',
        'feed.tomorrow_planned': 'На завтра запланировано:',
        'feed.alternative_topics': 'Альтернативные темы:',
        'feed.keep_or_change': 'Оставить как есть — просто нажмите ✅\nИзменить — напишите номер или свою тему',
        'feed.topic_saved': 'Тема на завтра сохранена!',
        'feed.topic_changed': 'Тема на завтра изменена:',
        'feed.whats_next': 'Что дальше?',
        'feed.upcoming_topics': 'Предстоящие темы:',
        'feed.week_progress': 'Прогресс недели: {current} из {total}',
        'feed.menu_title': 'Режим Лента',
        'feed.topics_menu_title': 'Темы недели',
        'feed.topics_edit_hint': 'Нажмите ✏️, чтобы изменить тему',
        'feed.no_digest_today': 'На сегодня дайджест уже прочитан. Приходите завтра!',
        'feed.enter_new_topic': 'Введите новую тему для дня {day}:',
        'feed.topic_updated': 'Тема для дня {day} обновлена:',
        'feed.ask_details': 'Хотите узнать подробнее? Задайте вопрос!',
        'buttons.keep_topic': 'Оставить как есть',
        'buttons.write_fixation': 'Написать фиксацию',
        'buttons.get_digest': 'Получить дайджест',
        'buttons.topics_menu': 'Темы недели',
        'buttons.edit_topic': '✏️',
        'buttons.back_to_menu': '« Назад',
        'buttons.select_topics': 'Выбрать темы на неделю',
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
        'commands.language': '/language — change language',
        'commands.help': '/help — help',

        # Menu descriptions
        'menu.learn': 'Get a new topic',
        'menu.progress': 'My progress',
        'menu.profile': 'My profile',
        'menu.update': 'Update profile',
        'menu.mode': 'Select mode',
        'menu.language': 'Change language',
        'menu.start': 'Restart onboarding',
        'menu.help': 'Help',

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

        # Duration
        'duration.minutes_5': '⚡ 5 minutes',
        'duration.minutes_15': '🕑 15 minutes',
        'duration.minutes_25': '🕓 25 minutes',

        # Buttons
        'buttons.yes': 'Yes',
        'buttons.cancel': 'Cancel',
        'buttons.start_now': 'Start now',
        'buttons.start_scheduled': 'Scheduled',
        'buttons.change_language': 'Change language',

        # Profile buttons
        'buttons.name': 'Name',
        'buttons.occupation': 'Occupation',
        'buttons.interests': 'Interests',
        'buttons.values': 'Values',
        'buttons.goals': 'Goals',
        'buttons.duration': 'Duration',
        'buttons.schedule': 'Schedule',
        'buttons.difficulty': 'Difficulty',
        'buttons.bot_mode': 'Bot mode',

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

        # Profile
        'profile.what_important': 'What matters',
        'profile.what_change': 'What to change',
        'profile.reminder_at': 'Reminder at',
        'profile.not_specified': 'not specified',
        'profile.first_start': 'First run /start',

        # Update profile
        'update.your_name': 'Your name',
        'update.whats_your_name': "What's your name?",
        'update.your_occupation': 'Your occupation',
        'update.whats_your_occupation': 'What do you do?',
        'update.your_interests': 'Your interests',
        'update.what_interests': 'What are your interests?',
        'update.your_goals': 'Your goals',
        'update.what_goals': 'What do you want to change in your life?',
        'update.current_time': 'Current duration',
        'update.how_many_minutes': 'How many minutes per topic?',
        'update.current_schedule': 'Current schedule',
        'update.when_remind': 'When should I remind you? (HH:MM)',
        'update.current_difficulty': 'Current difficulty level',
        'update.select_difficulty': 'Select difficulty level:',
        'update.saved': 'Saved!',
        'update.name_changed': 'Name changed',
        'update.occupation_changed': 'Occupation changed',
        'update.interests_changed': 'Interests updated',
        'update.goals_changed': 'Goals updated',
        'update.duration_changed': 'Duration changed',
        'update.schedule_changed': 'Schedule changed',
        'update.difficulty_changed': 'Difficulty changed',

        # Help
        'help.title': 'Main commands',
        'help.how_it_works': 'How learning works',
        'help.step1': '1. I send personalized material',
        'help.step2': '2. You study it (5-25 min)',
        'help.step3': '3. Answer a question to reinforce',
        'help.step4': '4. Topic counts toward progress',
        'help.schedule_note': "I'll send material at scheduled time or via /learn",
        'help.feedback': 'Feedback and suggestions',

        # Errors
        'errors.try_again': 'Try again',

        # Loading
        'loading.generating_topics': '⏳ Generating topics...',
        'loading.generating_content': '⏳ Preparing content...',
        'loading.processing': '⏳ Processing...',

        # Feed
        'feed.suggested_topics': 'Suggested Topics',
        'feed.select_hint': 'Select with buttons or type:',
        'feed.select_example': 'Example: "1, 3" or "topic 2 and also want mindfulness"',
        'feed.topics_selected': 'Topics selected!',
        'feed.topics_count': '{count} topics selected.',
        'feed.selected_topics': 'Selected topics:',
        'feed.your_topics': 'your topics',
        'feed.what_next': 'When do you want to start?',
        'feed.tomorrow_planned': 'Planned for tomorrow:',
        'feed.alternative_topics': 'Alternative topics:',
        'feed.keep_or_change': 'Keep as is — just press ✅\nChange — type a number or your own topic',
        'feed.topic_saved': 'Tomorrow\'s topic saved!',
        'feed.topic_changed': 'Tomorrow\'s topic changed:',
        'feed.whats_next': "What's next?",
        'feed.upcoming_topics': 'Upcoming topics:',
        'feed.week_progress': 'Week progress: {current} of {total}',
        'feed.menu_title': 'Feed Mode',
        'feed.topics_menu_title': 'Week Topics',
        'feed.topics_edit_hint': 'Press ✏️ to change a topic',
        'feed.no_digest_today': "Today's digest is already read. Come back tomorrow!",
        'feed.enter_new_topic': 'Enter new topic for day {day}:',
        'feed.topic_updated': 'Topic for day {day} updated:',
        'feed.ask_details': 'Want to learn more? Ask a question!',
        'buttons.keep_topic': 'Keep as is',
        'buttons.write_fixation': 'Write fixation',
        'buttons.get_digest': 'Get digest',
        'buttons.topics_menu': 'Week topics',
        'buttons.edit_topic': '✏️',
        'buttons.back_to_menu': '« Back',
        'buttons.select_topics': 'Select week topics',
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
        'commands.language': '/language — cambiar idioma',
        'commands.help': '/help — ayuda',

        # Descripciones de menú
        'menu.learn': 'Obtener un nuevo tema',
        'menu.progress': 'Mi progreso',
        'menu.profile': 'Mi perfil',
        'menu.update': 'Actualizar perfil',
        'menu.mode': 'Seleccionar modo',
        'menu.language': 'Cambiar idioma',
        'menu.start': 'Reiniciar onboarding',
        'menu.help': 'Ayuda',

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

        # Duración
        'duration.minutes_5': '⚡ 5 minutos',
        'duration.minutes_15': '🕑 15 minutos',
        'duration.minutes_25': '🕓 25 minutos',

        # Botones
        'buttons.yes': 'Sí',
        'buttons.cancel': 'Cancelar',
        'buttons.start_now': 'Empezar ahora',
        'buttons.start_scheduled': 'Programado',
        'buttons.change_language': 'Cambiar idioma',

        # Botones de perfil
        'buttons.name': 'Nombre',
        'buttons.occupation': 'Ocupación',
        'buttons.interests': 'Intereses',
        'buttons.values': 'Valores',
        'buttons.goals': 'Metas',
        'buttons.duration': 'Duración',
        'buttons.schedule': 'Horario',
        'buttons.difficulty': 'Dificultad',
        'buttons.bot_mode': 'Modo bot',

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

        # Perfil
        'profile.what_important': 'Qué es importante',
        'profile.what_change': 'Qué cambiar',
        'profile.reminder_at': 'Recordatorio a las',
        'profile.not_specified': 'no especificado',
        'profile.first_start': 'Primero /start',

        # Actualización de perfil
        'update.your_name': 'Tu nombre',
        'update.whats_your_name': '¿Cómo te llamas?',
        'update.your_occupation': 'Tu ocupación',
        'update.whats_your_occupation': '¿A qué te dedicas?',
        'update.your_interests': 'Tus intereses',
        'update.what_interests': '¿Cuáles son tus intereses?',
        'update.your_goals': 'Tus metas',
        'update.what_goals': '¿Qué quieres cambiar en tu vida?',
        'update.current_time': 'Duración actual',
        'update.how_many_minutes': '¿Cuántos minutos por tema?',
        'update.current_schedule': 'Horario actual',
        'update.when_remind': '¿Cuándo recordarte? (HH:MM)',
        'update.current_difficulty': 'Nivel de dificultad actual',
        'update.select_difficulty': 'Selecciona nivel de dificultad:',
        'update.saved': '¡Guardado!',
        'update.name_changed': 'Nombre cambiado',
        'update.occupation_changed': 'Ocupación cambiada',
        'update.interests_changed': 'Intereses actualizados',
        'update.goals_changed': 'Metas actualizadas',
        'update.duration_changed': 'Duración cambiada',
        'update.schedule_changed': 'Horario cambiado',
        'update.difficulty_changed': 'Dificultad cambiada',

        # Ayuda
        'help.title': 'Comandos principales',
        'help.how_it_works': 'Cómo funciona el aprendizaje',
        'help.step1': '1. Envío material personalizado',
        'help.step2': '2. Lo estudias (5-25 min)',
        'help.step3': '3. Respondes una pregunta para reforzar',
        'help.step4': '4. El tema cuenta para tu progreso',
        'help.schedule_note': 'Enviaré material a la hora programada o via /learn',
        'help.feedback': 'Comentarios y sugerencias',

        # Errores
        'errors.try_again': 'Inténtalo de nuevo',

        # Cargando
        'loading.generating_topics': '⏳ Generando temas...',
        'loading.generating_content': '⏳ Preparando contenido...',
        'loading.processing': '⏳ Procesando...',

        # Feed
        'feed.suggested_topics': 'Temas Sugeridos',
        'feed.select_hint': 'Selecciona con botones o escribe:',
        'feed.select_example': 'Ejemplo: "1, 3" o "tema 2 y también quiero atención plena"',
        'feed.topics_selected': '¡Temas seleccionados!',
        'feed.topics_count': '{count} temas seleccionados.',
        'feed.selected_topics': 'Temas seleccionados:',
        'feed.your_topics': 'tus temas',
        'feed.what_next': '¿Cuándo quieres empezar?',
        'feed.tomorrow_planned': 'Planificado para mañana:',
        'feed.alternative_topics': 'Temas alternativos:',
        'feed.keep_or_change': 'Mantener así — solo presiona ✅\nCambiar — escribe un número o tu propio tema',
        'feed.topic_saved': '¡Tema de mañana guardado!',
        'feed.topic_changed': 'Tema de mañana cambiado:',
        'feed.whats_next': '¿Qué sigue?',
        'feed.upcoming_topics': 'Próximos temas:',
        'feed.week_progress': 'Progreso semanal: {current} de {total}',
        'feed.menu_title': 'Modo Cinta',
        'feed.topics_menu_title': 'Temas de la semana',
        'feed.topics_edit_hint': 'Presiona ✏️ para cambiar un tema',
        'feed.no_digest_today': 'El resumen de hoy ya fue leído. ¡Vuelve mañana!',
        'feed.enter_new_topic': 'Ingresa nuevo tema para el día {day}:',
        'feed.topic_updated': 'Tema para el día {day} actualizado:',
        'feed.ask_details': '¿Quieres saber más? ¡Haz una pregunta!',
        'buttons.keep_topic': 'Mantener así',
        'buttons.write_fixation': 'Escribir fijación',
        'buttons.get_digest': 'Obtener resumen',
        'buttons.topics_menu': 'Temas de la semana',
        'buttons.edit_topic': '✏️',
        'buttons.back_to_menu': '« Volver',
        'buttons.select_topics': 'Seleccionar temas de la semana',
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
