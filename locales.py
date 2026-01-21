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
        'commands.mode': '/mode — выбрать режим',
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
        'buttons.bonus_yes': '🚀 Да, давай сложнее!',
        'buttons.bonus_no': '✅ Достаточно',
        'buttons.skip_topic': '⏭ Пропустить тему',
        'buttons.skip_practice': '⏭ Пропустить практику',
        'buttons.start_today': '🚀 Начать сегодня',
        'buttons.start_tomorrow': '📅 Завтра',
        'buttons.start_day_after': '📅 Послезавтра',

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

        # Уровни опыта
        'experience.student': 'Студент',
        'experience.student_desc': 'Учусь или недавно закончил',
        'experience.junior': 'Junior',
        'experience.junior_desc': '0-2 года опыта',
        'experience.middle': 'Middle',
        'experience.middle_desc': '2-5 лет опыта',
        'experience.senior': 'Senior',
        'experience.senior_desc': '5+ лет опыта',
        'experience.switching': 'Меняю сферу',
        'experience.switching_desc': 'Перехожу из другой области',

        # Уровни сложности материала
        'difficulty.easy': 'Начальный',
        'difficulty.easy_desc': 'С нуля, простым языком',
        'difficulty.medium': 'Средний',
        'difficulty.medium_desc': 'Есть базовые знания',
        'difficulty.hard': 'Продвинутый',
        'difficulty.hard_desc': 'Глубокое погружение',

        # Стили обучения
        'learning_style.theoretical': 'Теоретик',
        'learning_style.theoretical_desc': 'Сначала теория, потом практика',
        'learning_style.practical': 'Практик',
        'learning_style.practical_desc': 'Учусь на примерах и задачах',
        'learning_style.mixed': 'Смешанный',
        'learning_style.mixed_desc': 'Баланс теории и практики',

        # Уровни сложности вопросов (Блум)
        'bloom.level_1': 'Знание',
        'bloom.level_1_short': 'Сложность-1',
        'bloom.level_1_desc': 'Различение и запоминание понятий',
        'bloom.level_2': 'Понимание',
        'bloom.level_2_short': 'Сложность-2',
        'bloom.level_2_desc': 'Открытые вопросы на понимание',
        'bloom.level_3': 'Применение',
        'bloom.level_3_short': 'Сложность-3',
        'bloom.level_3_desc': 'Анализ и примеры из практики',

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
        'update.difficulty_scale': 'Шкала сложности',
        'update.easiest': 'самый лёгкий',
        'update.hardest': 'самый сложный',
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
        # Прогресс обработки вопроса
        'loading.progress.analyzing': '🔍 Анализирую вопрос...',
        'loading.progress.searching': '📚 Ищу в базе знаний...',
        'loading.progress.generating': '✨ Генерирую ответ...',
        'loading.progress.done': '✅ Готово!',

        # Марафон
        'marathon.topic_completed': 'Тема засчитана!',
        'marathon.level_up': 'Поздравляем! Вы перешли на',
        'marathon.next_topic': 'Следующая тема',
        'marathon.want_harder': 'Хотите дополнительный вопрос посложнее?',
        'marathon.next_command': '/learn — следующая тема',
        'marathon.generating_harder': 'Генерирую вопрос посложнее...',
        'marathon.bonus_question': 'Бонусный вопрос',
        'marathon.write_answer': 'Напишите ответ 👇',
        'marathon.ok': 'Хорошо!',
        'marathon.bonus_completed': 'Отлично! Бонусный вопрос засчитан!',
        'marathon.training_skills': 'Вы тренируете навыки',
        'marathon.and_higher': 'и выше.',
        'marathon.generating_material': 'Генерирую персональный материал...',
        'marathon.day_theory': 'День {day} — Теория',
        'marathon.minutes': '{minutes} минут',
        'marathon.reflection_question': 'Вопрос для размышления',
        'marathon.answer_hint': 'Напишите ответ в сообщении. Он не проверяется — после получения любого ответа тема считается пройденной.',
        'marathon.preparing_practice': 'Готовлю практическое задание...',
        'marathon.day_practice': 'День {day} — Практика',
        'marathon.task': 'Задание',
        'marathon.work_product': 'Рабочий продукт',
        'marathon.submit_hint': 'Напишите, что сделали. Минимум — пару предложений о рабочем продукте.',
        'marathon.wp_examples': 'Примеры РП',
        'marathon.when_complete': 'Когда выполните задание',
        'marathon.write_wp_name': 'Напишите название своего рабочего продукта.',
        'marathon.example': 'Например',
        'marathon.no_check_hint': 'Проверки нет — просто напишите, что сделали, и практика засчитается.',

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
        'buttons.reset_topics': 'Сгенерировать заново',
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
        'commands.mode': '/mode — select mode',
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
        'buttons.bonus_yes': '🚀 Yes, let\'s go harder!',
        'buttons.bonus_no': '✅ Enough',
        'buttons.skip_topic': '⏭ Skip topic',
        'buttons.skip_practice': '⏭ Skip practice',
        'buttons.start_today': '🚀 Start today',
        'buttons.start_tomorrow': '📅 Tomorrow',
        'buttons.start_day_after': '📅 Day after',

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

        # Experience levels
        'experience.student': 'Student',
        'experience.student_desc': 'Studying or recently graduated',
        'experience.junior': 'Junior',
        'experience.junior_desc': '0-2 years of experience',
        'experience.middle': 'Middle',
        'experience.middle_desc': '2-5 years of experience',
        'experience.senior': 'Senior',
        'experience.senior_desc': '5+ years of experience',
        'experience.switching': 'Career switch',
        'experience.switching_desc': 'Transitioning from another field',

        # Difficulty levels
        'difficulty.easy': 'Beginner',
        'difficulty.easy_desc': 'From scratch, simple language',
        'difficulty.medium': 'Intermediate',
        'difficulty.medium_desc': 'Have basic knowledge',
        'difficulty.hard': 'Advanced',
        'difficulty.hard_desc': 'Deep dive',

        # Learning styles
        'learning_style.theoretical': 'Theorist',
        'learning_style.theoretical_desc': 'Theory first, then practice',
        'learning_style.practical': 'Practitioner',
        'learning_style.practical_desc': 'Learn by examples and tasks',
        'learning_style.mixed': 'Mixed',
        'learning_style.mixed_desc': 'Balance of theory and practice',

        # Bloom levels (question complexity)
        'bloom.level_1': 'Knowledge',
        'bloom.level_1_short': 'Level 1',
        'bloom.level_1_desc': 'Distinguishing and memorizing concepts',
        'bloom.level_2': 'Comprehension',
        'bloom.level_2_short': 'Level 2',
        'bloom.level_2_desc': 'Open questions for understanding',
        'bloom.level_3': 'Application',
        'bloom.level_3_short': 'Level 3',
        'bloom.level_3_desc': 'Analysis and practical examples',

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
        'update.difficulty_scale': 'Difficulty scale',
        'update.easiest': 'easiest',
        'update.hardest': 'hardest',
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
        # Question processing progress
        'loading.progress.analyzing': '🔍 Analyzing question...',
        'loading.progress.searching': '📚 Searching knowledge base...',
        'loading.progress.generating': '✨ Generating answer...',
        'loading.progress.done': '✅ Done!',

        # Marathon
        'marathon.topic_completed': 'Topic completed!',
        'marathon.level_up': 'Congratulations! You advanced to',
        'marathon.next_topic': 'Next topic',
        'marathon.want_harder': 'Want a harder bonus question?',
        'marathon.next_command': '/learn — next topic',
        'marathon.generating_harder': 'Generating a harder question...',
        'marathon.bonus_question': 'Bonus question',
        'marathon.write_answer': 'Write your answer 👇',
        'marathon.ok': 'OK!',
        'marathon.bonus_completed': 'Great! Bonus question completed!',
        'marathon.training_skills': 'You are training skills',
        'marathon.and_higher': 'and above.',
        'marathon.generating_material': 'Generating personalized material...',
        'marathon.day_theory': 'Day {day} — Theory',
        'marathon.minutes': '{minutes} minutes',
        'marathon.reflection_question': 'Reflection question',
        'marathon.answer_hint': 'Write your answer in a message. It is not graded — the topic is marked complete after any answer.',
        'marathon.preparing_practice': 'Preparing practice task...',
        'marathon.day_practice': 'Day {day} — Practice',
        'marathon.task': 'Task',
        'marathon.work_product': 'Work product',
        'marathon.submit_hint': 'Write what you did. Minimum — a couple of sentences about your work product.',
        'marathon.wp_examples': 'Work product examples',
        'marathon.when_complete': 'When you complete the task',
        'marathon.write_wp_name': 'Write the name of your work product.',
        'marathon.example': 'For example',
        'marathon.no_check_hint': 'No grading — just write what you did, and the practice will be marked complete.',

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
        'buttons.reset_topics': 'Regenerate topics',
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
        'commands.mode': '/mode — seleccionar modo',
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
        'buttons.bonus_yes': '🚀 ¡Sí, más difícil!',
        'buttons.bonus_no': '✅ Suficiente',
        'buttons.skip_topic': '⏭ Saltar tema',
        'buttons.skip_practice': '⏭ Saltar práctica',
        'buttons.start_today': '🚀 Empezar hoy',
        'buttons.start_tomorrow': '📅 Mañana',
        'buttons.start_day_after': '📅 Pasado mañana',

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

        # Niveles de experiencia
        'experience.student': 'Estudiante',
        'experience.student_desc': 'Estudiando o recién graduado',
        'experience.junior': 'Junior',
        'experience.junior_desc': '0-2 años de experiencia',
        'experience.middle': 'Middle',
        'experience.middle_desc': '2-5 años de experiencia',
        'experience.senior': 'Senior',
        'experience.senior_desc': '5+ años de experiencia',
        'experience.switching': 'Cambio de carrera',
        'experience.switching_desc': 'Transición desde otro campo',

        # Niveles de dificultad
        'difficulty.easy': 'Principiante',
        'difficulty.easy_desc': 'Desde cero, lenguaje simple',
        'difficulty.medium': 'Intermedio',
        'difficulty.medium_desc': 'Tengo conocimientos básicos',
        'difficulty.hard': 'Avanzado',
        'difficulty.hard_desc': 'Inmersión profunda',

        # Estilos de aprendizaje
        'learning_style.theoretical': 'Teórico',
        'learning_style.theoretical_desc': 'Primero teoría, luego práctica',
        'learning_style.practical': 'Práctico',
        'learning_style.practical_desc': 'Aprendo con ejemplos y tareas',
        'learning_style.mixed': 'Mixto',
        'learning_style.mixed_desc': 'Balance de teoría y práctica',

        # Niveles de Bloom (complejidad de preguntas)
        'bloom.level_1': 'Conocimiento',
        'bloom.level_1_short': 'Nivel 1',
        'bloom.level_1_desc': 'Distinguir y memorizar conceptos',
        'bloom.level_2': 'Comprensión',
        'bloom.level_2_short': 'Nivel 2',
        'bloom.level_2_desc': 'Preguntas abiertas para comprensión',
        'bloom.level_3': 'Aplicación',
        'bloom.level_3_short': 'Nivel 3',
        'bloom.level_3_desc': 'Análisis y ejemplos prácticos',

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
        'update.difficulty_scale': 'Escala de dificultad',
        'update.easiest': 'más fácil',
        'update.hardest': 'más difícil',
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
        # Progreso de procesamiento de pregunta
        'loading.progress.analyzing': '🔍 Analizando pregunta...',
        'loading.progress.searching': '📚 Buscando en base de conocimientos...',
        'loading.progress.generating': '✨ Generando respuesta...',
        'loading.progress.done': '✅ ¡Listo!',

        # Maratón
        'marathon.topic_completed': '¡Tema completado!',
        'marathon.level_up': '¡Felicidades! Has avanzado a',
        'marathon.next_topic': 'Próximo tema',
        'marathon.want_harder': '¿Quieres una pregunta extra más difícil?',
        'marathon.next_command': '/learn — próximo tema',
        'marathon.generating_harder': 'Generando una pregunta más difícil...',
        'marathon.bonus_question': 'Pregunta extra',
        'marathon.write_answer': 'Escribe tu respuesta 👇',
        'marathon.ok': '¡OK!',
        'marathon.bonus_completed': '¡Genial! ¡Pregunta extra completada!',
        'marathon.training_skills': 'Estás entrenando habilidades',
        'marathon.and_higher': 'y superiores.',
        'marathon.generating_material': 'Generando material personalizado...',
        'marathon.day_theory': 'Día {day} — Teoría',
        'marathon.minutes': '{minutes} minutos',
        'marathon.reflection_question': 'Pregunta de reflexión',
        'marathon.answer_hint': 'Escribe tu respuesta en un mensaje. No se califica — el tema se marca como completado después de cualquier respuesta.',
        'marathon.preparing_practice': 'Preparando tarea práctica...',
        'marathon.day_practice': 'Día {day} — Práctica',
        'marathon.task': 'Tarea',
        'marathon.work_product': 'Producto de trabajo',
        'marathon.submit_hint': 'Escribe lo que hiciste. Mínimo — un par de oraciones sobre tu producto de trabajo.',
        'marathon.wp_examples': 'Ejemplos de producto de trabajo',
        'marathon.when_complete': 'Cuando completes la tarea',
        'marathon.write_wp_name': 'Escribe el nombre de tu producto de trabajo.',
        'marathon.example': 'Por ejemplo',
        'marathon.no_check_hint': 'Sin calificación — solo escribe lo que hiciste, y la práctica se marcará como completada.',

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
        'buttons.reset_topics': 'Regenerar temas',
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
