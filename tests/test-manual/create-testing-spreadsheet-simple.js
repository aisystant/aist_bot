/**
 * УПРОЩЁННАЯ версия скрипта для быстрого создания таблицы тестирования
 *
 * Эта версия работает быстрее за счёт:
 * - Минимума форматирования
 * - Пакетной записи данных
 * - Отсутствия условного форматирования (можно добавить вручную)
 */

function createTestingSpreadsheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  // Создаём листы
  createSummarySheet(ss);
  createScenariosSheets(ss);
  createBugsSheet(ss);

  SpreadsheetApp.getUi().alert('✅ Таблица создана!');
}

/**
 * Лист сводки
 */
function createSummarySheet(ss) {
  let sheet = ss.getSheetByName('Сводка');
  if (!sheet) {
    sheet = ss.insertSheet('Сводка', 0);
  } else {
    sheet.clear();
  }

  const data = [
    ['СВОДКА ТЕСТИРОВАНИЯ', '', '', '', '', ''],
    ['', '', '', '', '', ''],
    ['Дата:', '', 'Тестировщик:', '', '', ''],
    ['Версия бота:', '', 'Устройство:', '', '', ''],
    ['', '', '', '', '', ''],
    ['Раздел', 'Всего', '✅ OK', '⚠️ Замеч.', '❌ Баг', '⏭️ Пропуск'],
    ['1. Регистрация', '12', '', '', '', ''],
    ['2. Марафон', '17', '', '', '', ''],
    ['3. Лента', '14', '', '', '', ''],
    ['4. Режимы', '10', '', '', '', ''],
    ['5. Настройки', '12', '', '', '', ''],
    ['6. Язык', '8', '', '', '', ''],
    ['7. Команды', '10', '', '', '', ''],
    ['8. Граничные случаи', '12', '', '', '', ''],
    ['9. Качество ИИ', '10', '', '', '', ''],
    ['10. Напоминания', '8', '', '', '', ''],
    ['11. Прогресс', '10', '', '', '', ''],
    ['ИТОГО', '123', '', '', '', ''],
  ];

  sheet.getRange(1, 1, data.length, 6).setValues(data);
  sheet.getRange('A1').setFontWeight('bold');
  sheet.getRange('A6:F6').setFontWeight('bold').setBackground('#d9d9d9');
  sheet.getRange('A18:F18').setFontWeight('bold').setBackground('#d9d9d9');
}

/**
 * Создание листов для каждого класса сценариев
 */
function createScenariosSheets(ss) {
  const allScenarios = getScenariosData();

  allScenarios.forEach((section, index) => {
    const sheetName = `${index + 1}. ${section.name}`;
    let sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      sheet = ss.insertSheet(sheetName);
    } else {
      sheet.clear();
    }

    // Заголовок
    const headers = ['№', 'Приор.', 'Название', 'Предусловия', 'Ожидаемый результат', 'Статус', 'Комментарий'];

    // Данные
    const rows = section.scenarios.map(s => [
      s.id,
      s.critical ? '🔥' : '📋',
      s.name,
      s.preconditions || '—',
      s.expected,
      '',
      ''
    ]);

    // Всё одним вызовом
    const allData = [
      [`РАЗДЕЛ ${index + 1}: ${section.name.toUpperCase()}`, '', '', '', '', '', ''],
      headers,
      ...rows
    ];

    sheet.getRange(1, 1, allData.length, 7).setValues(allData);
    sheet.getRange('A1').setFontWeight('bold');
    sheet.getRange('A2:G2').setFontWeight('bold').setBackground('#d9d9d9');

    // Валидация статуса - одним вызовом
    const statusRule = SpreadsheetApp.newDataValidation()
      .requireValueInList(['✅ OK', '⚠️ Замечание', '❌ Баг', '⏭️ Пропущен'], true)
      .build();
    sheet.getRange(3, 6, rows.length, 1).setDataValidation(statusRule);
  });
}

/**
 * Лист для багов
 */
function createBugsSheet(ss) {
  let sheet = ss.getSheetByName('Баги');
  if (!sheet) {
    sheet = ss.insertSheet('Баги');
  } else {
    sheet.clear();
  }

  const data = [
    ['СПИСОК БАГОВ', '', '', '', '', '', '', ''],
    ['ID', 'Серьёзность', 'Сценарий', 'Описание', 'Шаги', 'Ожидалось', 'Фактически', 'Статус'],
    ['BUG-001', '', '', '', '', '', '', ''],
    ['BUG-002', '', '', '', '', '', '', ''],
    ['BUG-003', '', '', '', '', '', '', ''],
  ];

  sheet.getRange(1, 1, data.length, 8).setValues(data);
  sheet.getRange('A1').setFontWeight('bold');
  sheet.getRange('A2:H2').setFontWeight('bold').setBackground('#f4cccc');
}

/**
 * Данные сценариев (сокращённые)
 */
function getScenariosData() {
  return [
    {
      name: 'Регистрация',
      scenarios: [
        { id: '1.1', name: 'Первый запуск бота', critical: true, preconditions: 'Новый пользователь', expected: 'Приветствие, выбор языка' },
        { id: '1.2', name: 'Выбор языка', critical: true, preconditions: '1.1 пройден', expected: 'Язык сохранён' },
        { id: '1.3', name: 'Ввод имени', critical: true, preconditions: 'Язык выбран', expected: 'Имя принято' },
        { id: '1.4', name: 'Ввод занятия', critical: false, preconditions: 'Имя введено', expected: 'Занятие сохранено' },
        { id: '1.5', name: 'Ввод интересов', critical: false, preconditions: 'Занятие введено', expected: 'Интересы сохранены' },
        { id: '1.6', name: 'Выбор режима (Марафон)', critical: true, preconditions: 'Интересы введены', expected: 'Марафон активирован' },
        { id: '1.7', name: 'Выбор режима (Лента)', critical: true, preconditions: 'Интересы введены', expected: 'Лента активирована' },
        { id: '1.8', name: 'Повторный /start', critical: false, preconditions: 'Пользователь существует', expected: 'Приветствие, НЕ онбординг' },
        { id: '1.9', name: 'Пустое имя', critical: false, preconditions: 'Запрошено имя', expected: 'Просьба ввести заново' },
        { id: '1.10', name: 'Длинное имя (200+)', critical: false, preconditions: 'Запрошено имя', expected: 'Обрезается/отклоняется' },
        { id: '1.11', name: 'Прерывание онбординга', critical: false, preconditions: 'Онбординг не завершён', expected: 'Продолжение или /start' },
        { id: '1.12', name: '/start во время онбординга', critical: false, preconditions: 'Онбординг в процессе', expected: 'Перезапуск' },
      ]
    },
    {
      name: 'Марафон',
      scenarios: [
        { id: '2.1', name: 'Получение первого Урока', critical: true, preconditions: 'Режим Марафон', expected: 'Материал Урока' },
        { id: '2.2', name: 'Переход к вопросу Урока', critical: true, preconditions: 'Материал получен', expected: 'Вопрос по Сложности' },
        { id: '2.3', name: 'Ответ на вопрос Урока', critical: true, preconditions: 'Вопрос получен', expected: 'Обратная связь' },
        { id: '2.4', name: 'Получение Задания', critical: true, preconditions: 'Урок пройден', expected: 'Материал Задания' },
        { id: '2.5', name: 'Ответ на Задание', critical: true, preconditions: 'Задание получено', expected: 'Обратная связь' },
        { id: '2.6', name: 'Бонусный вопрос', critical: true, preconditions: 'Ответ оценён', expected: 'Предложен бонус' },
        { id: '2.7', name: 'Ответ на бонус', critical: false, preconditions: 'Бонус получен', expected: 'Оценка' },
        { id: '2.8', name: 'Пропуск бонуса', critical: true, preconditions: 'Бонус предложен', expected: 'Переход без штрафа' },
        { id: '2.9', name: 'Запрос подсказки', critical: false, preconditions: 'Вопрос активен', expected: 'Подсказка' },
        { id: '2.10', name: '/progress', critical: false, preconditions: 'Есть прогресс', expected: 'Статистика' },
        { id: '2.11', name: 'Завершение (день 14)', critical: false, preconditions: 'День 14', expected: 'Поздравление' },
        { id: '2.12', name: 'Лимит уроков', critical: false, preconditions: 'Лимит достигнут', expected: 'Сообщение' },
        { id: '2.13', name: 'Вопрос к ИИ (?)', critical: true, preconditions: 'Марафон активен', expected: 'Консультация' },
        { id: '2.14', name: 'После ? вопроса', critical: true, preconditions: 'Консультация получена', expected: 'Продолжение' },
        { id: '2.15', name: 'Изменение Сложности', critical: false, preconditions: 'Марафон активен', expected: 'Новая Сложность' },
        { id: '2.16', name: 'Сброс Марафона', critical: false, preconditions: 'Есть прогресс', expected: 'Прогресс сброшен' },
        { id: '2.17', name: 'Отмена сброса', critical: false, preconditions: 'Диалог сброса', expected: 'Прогресс сохранён' },
      ]
    },
    {
      name: 'Лента',
      scenarios: [
        { id: '3.1', name: 'Активация (темы не выбраны)', critical: true, preconditions: 'Режим Лента', expected: 'Выбор тем' },
        { id: '3.2', name: 'Выбор тем (1-3)', critical: true, preconditions: 'Меню тем', expected: 'Темы сохранены' },
        { id: '3.3', name: 'Лимит тем (4+)', critical: false, preconditions: '3 темы выбрано', expected: 'Ограничение' },
        { id: '3.4', name: 'Получение Дайджеста', critical: true, preconditions: 'Темы выбраны', expected: 'Дайджест' },
        { id: '3.5', name: 'Фиксация', critical: true, preconditions: 'Дайджест получен', expected: 'Обратная связь' },
        { id: '3.6', name: 'Углубление темы', critical: true, preconditions: 'Несколько дайджестов', expected: 'Сложнее' },
        { id: '3.7', name: 'Завершение недели', critical: false, preconditions: 'Неделя пройдена', expected: 'Статистика' },
        { id: '3.8', name: 'Смена тем', critical: true, preconditions: 'Неделя активна', expected: 'Темы обновлены' },
        { id: '3.9', name: 'Лимит на день', critical: false, preconditions: 'Дайджест пройден', expected: 'Сообщение' },
        { id: '3.10', name: 'Пустая фиксация', critical: false, preconditions: 'Вопрос получен', expected: 'Просьба написать' },
        { id: '3.11', name: 'Длинная фиксация', critical: false, preconditions: 'Вопрос получен', expected: 'Принята' },
        { id: '3.12', name: 'Марафон → Лента', critical: true, preconditions: 'Был в Марафоне', expected: 'Лента работает' },
        { id: '3.13', name: '/learn без тем', critical: false, preconditions: 'Темы не выбраны', expected: 'Предложение выбрать' },
        { id: '3.14', name: 'История дайджестов', critical: false, preconditions: 'Есть прогресс', expected: 'Видны пройденные' },
      ]
    },
    {
      name: 'Режимы',
      scenarios: [
        { id: '4.1', name: '/mode', critical: true, preconditions: 'Зарегистрирован', expected: 'Меню режимов' },
        { id: '4.2', name: 'Марафон → Лента', critical: true, preconditions: 'Режим Марафон', expected: 'Лента активна' },
        { id: '4.3', name: 'Лента → Марафон', critical: true, preconditions: 'Режим Лента', expected: 'Марафон активен' },
        { id: '4.4', name: 'Текущий режим (Марафон)', critical: false, preconditions: 'В Марафоне', expected: 'Настройки' },
        { id: '4.5', name: 'Текущий режим (Лента)', critical: false, preconditions: 'В Ленте', expected: 'Настройки' },
        { id: '4.6', name: 'Сохранение прогресса', critical: false, preconditions: 'Марафон 10+ дней', expected: 'Прогресс сохранён' },
        { id: '4.7', name: 'Экран Марафона', critical: false, preconditions: 'Марафон выбран', expected: 'Кнопки управления' },
        { id: '4.8', name: 'Экран Ленты', critical: false, preconditions: 'Лента выбрана', expected: 'Кнопки управления' },
        { id: '4.9', name: '/mode во время урока', critical: false, preconditions: 'Урок активен', expected: 'Меню или предупреждение' },
        { id: '4.10', name: 'Многократное переключение', critical: false, preconditions: 'Любой режим', expected: 'Всё работает' },
      ]
    },
    {
      name: 'Настройки',
      scenarios: [
        { id: '5.1', name: '/update', critical: true, preconditions: 'Зарегистрирован', expected: 'Профиль с кнопками' },
        { id: '5.2', name: 'Изменение имени', critical: false, preconditions: '/update открыт', expected: 'Имя обновлено' },
        { id: '5.3', name: 'Изменение занятия', critical: false, preconditions: '/update открыт', expected: 'Занятие обновлено' },
        { id: '5.4', name: 'Изменение интересов', critical: false, preconditions: '/update открыт', expected: 'Интересы обновлены' },
        { id: '5.5', name: 'Сложность 1', critical: false, preconditions: '/update открыт', expected: 'Различение' },
        { id: '5.6', name: 'Сложность 2', critical: false, preconditions: '/update открыт', expected: 'Понимание' },
        { id: '5.7', name: 'Сложность 3', critical: false, preconditions: '/update открыт', expected: 'Применение' },
        { id: '5.8', name: 'Напоминания (время)', critical: false, preconditions: 'Режим активен', expected: 'Время сохранено' },
        { id: '5.9', name: 'Несколько напоминаний', critical: false, preconditions: 'Настройки', expected: 'Оба времени' },
        { id: '5.10', name: 'Ошибка времени', critical: false, preconditions: 'Настройки', expected: 'Валидация' },
        { id: '5.11', name: 'Отмена редактирования', critical: false, preconditions: 'Редактирование', expected: 'Возврат' },
        { id: '5.12', name: 'Режим из /update', critical: false, preconditions: '/update открыт', expected: 'Переход в /mode' },
      ]
    },
    {
      name: 'Язык',
      scenarios: [
        { id: '6.1', name: 'Русский → Английский', critical: true, preconditions: 'Язык: ru', expected: 'Интерфейс на en' },
        { id: '6.2', name: 'Английский → Испанский', critical: true, preconditions: 'Любой язык', expected: 'Интерфейс на es' },
        { id: '6.3', name: 'Возврат к русскому', critical: false, preconditions: 'Язык: en/es', expected: 'Интерфейс на ru' },
        { id: '6.4', name: 'Сохранение языка', critical: true, preconditions: 'Язык: en', expected: 'После /start на en' },
        { id: '6.5', name: '/help на en', critical: false, preconditions: 'Язык: en', expected: 'Описания на en' },
        { id: '6.6', name: 'Ошибки на en', critical: false, preconditions: 'Язык: en', expected: 'Сообщения на en' },
        { id: '6.7', name: '/progress на es', critical: false, preconditions: 'Язык: es', expected: 'Статистика на es' },
        { id: '6.8', name: 'Контент vs интерфейс', critical: false, preconditions: 'Язык: en', expected: 'Документировать' },
      ]
    },
    {
      name: 'Команды',
      scenarios: [
        { id: '7.1', name: '/start', critical: true, preconditions: 'Любой', expected: 'Приветствие/меню' },
        { id: '7.2', name: '/help', critical: true, preconditions: 'Любой', expected: 'Список команд' },
        { id: '7.3', name: '/learn', critical: true, preconditions: 'Любой режим', expected: 'Урок/дайджест' },
        { id: '7.4', name: '/update', critical: true, preconditions: 'Зарегистрирован', expected: 'Профиль' },
        { id: '7.5', name: '/mode', critical: true, preconditions: 'Зарегистрирован', expected: 'Меню режимов' },
        { id: '7.6', name: '/progress', critical: true, preconditions: 'Зарегистрирован', expected: 'Статистика' },
        { id: '7.7', name: 'Неизвестная команда', critical: false, preconditions: 'Любой', expected: 'Игнор или /help' },
        { id: '7.8', name: 'Параметры (/start ref)', critical: false, preconditions: 'Любой', expected: 'Обработка' },
        { id: '7.9', name: 'Быстрые команды', critical: false, preconditions: 'Любой', expected: 'Все обработаны' },
        { id: '7.10', name: 'Команда вместо ввода', critical: false, preconditions: 'Ожидается текст', expected: 'Обработка/игнор' },
      ]
    },
    {
      name: 'Граничные случаи',
      scenarios: [
        { id: '8.1', name: 'Пустое сообщение', critical: false, preconditions: 'Любой', expected: 'Игнор/просьба' },
        { id: '8.2', name: 'Длинное (4000+ симв)', critical: false, preconditions: 'Любой', expected: 'Обработка' },
        { id: '8.3', name: 'Эмодзи', critical: false, preconditions: 'Любой', expected: 'Корректно' },
        { id: '8.4', name: 'Нелатиница', critical: false, preconditions: 'Любой', expected: 'Корректно' },
        { id: '8.5', name: 'Спам кнопок', critical: false, preconditions: 'Кнопки', expected: 'Debounce' },
        { id: '8.6', name: 'Старая кнопка', critical: false, preconditions: 'Старое сообщение', expected: 'Устарела' },
        { id: '8.7', name: 'Изображение', critical: true, preconditions: 'Ожидается текст', expected: 'Просьба текст' },
        { id: '8.8', name: 'Голосовое', critical: true, preconditions: 'Ожидается текст', expected: 'Просьба текст' },
        { id: '8.9', name: 'Стикер', critical: false, preconditions: 'Любой', expected: 'Игнор' },
        { id: '8.10', name: 'Параллельные сессии', critical: false, preconditions: '2 устройства', expected: 'Консистентно' },
        { id: '8.11', name: 'Удаление сообщения', critical: false, preconditions: 'Сообщение с кнопками', expected: 'Продолжение' },
        { id: '8.12', name: 'Блокировка бота', critical: false, preconditions: 'Бот заблокирован', expected: 'Данные сохранены' },
      ]
    },
    {
      name: 'Качество ИИ',
      scenarios: [
        { id: '9.1', name: 'Правильный ответ', critical: true, preconditions: 'Вопрос урока', expected: 'Положительная оценка' },
        { id: '9.2', name: 'Неправильный ответ', critical: true, preconditions: 'Вопрос урока', expected: 'Указаны ошибки' },
        { id: '9.3', name: 'Частично правильный', critical: true, preconditions: 'Вопрос урока', expected: 'Что верно/упущено' },
        { id: '9.4', name: 'Вопрос по теме', critical: true, preconditions: 'Любой', expected: 'Содержательный ответ' },
        { id: '9.5', name: 'Вопрос не по теме', critical: false, preconditions: 'Любой', expected: 'Мягкий отказ' },
        { id: '9.6', name: 'Подсказка', critical: false, preconditions: 'Вопрос активен', expected: 'Наводящая' },
        { id: '9.7', name: 'Фиксация (Лента)', critical: false, preconditions: 'Дайджест пройден', expected: 'Персональная ОС' },
        { id: '9.8', name: 'Оценка РП', critical: false, preconditions: 'Задание', expected: 'По существу' },
        { id: '9.9', name: 'Неприемлемый текст', critical: false, preconditions: 'Любой', expected: 'Корректная реакция' },
        { id: '9.10', name: 'Время ответа', critical: false, preconditions: 'Ответ отправлен', expected: '5-15 секунд' },
      ]
    },
    {
      name: 'Напоминания',
      scenarios: [
        { id: '10.1', name: 'Получение напоминания', critical: false, preconditions: 'Время 09:00', expected: 'Напоминание пришло' },
        { id: '10.2', name: 'Два напоминания', critical: false, preconditions: '09:00 и 18:00', expected: 'Оба пришли' },
        { id: '10.3', name: 'Отключение', critical: false, preconditions: 'Напоминания активны', expected: 'Не приходят' },
        { id: '10.4', name: 'После прохождения', critical: false, preconditions: 'Урок пройден', expected: 'Учитывает прогресс' },
        { id: '10.5', name: 'На паузе', critical: false, preconditions: 'Марафон на паузе', expected: 'Документировать' },
        { id: '10.6', name: 'Часовой пояс', critical: false, preconditions: 'Разные зоны', expected: 'Документировать' },
        { id: '10.7', name: 'Бот заблокирован', critical: false, preconditions: 'Напоминание', expected: 'Нет краша' },
        { id: '10.8', name: 'Массовая отправка', critical: false, preconditions: '10+ пользователей', expected: 'Все получили' },
      ]
    },
    {
      name: 'Прогресс',
      scenarios: [
        { id: '11.1', name: '/progress', critical: true, preconditions: 'Любой', expected: 'Статистика' },
        { id: '11.2', name: 'Статистика Марафона', critical: false, preconditions: 'Режим Марафон', expected: 'День, уроки, %' },
        { id: '11.3', name: 'Статистика Ленты', critical: false, preconditions: 'Режим Лента', expected: 'Дайджесты, темы' },
        { id: '11.4', name: 'Нулевой прогресс', critical: false, preconditions: 'Только зарегистрирован', expected: '0 уроков' },
        { id: '11.5', name: 'После переключения', critical: false, preconditions: 'Был прогресс', expected: 'Текущий режим' },
        { id: '11.6', name: 'Активность по дням', critical: false, preconditions: 'Несколько дней', expected: 'Видна' },
        { id: '11.7', name: 'Прогресс по темам', critical: false, preconditions: '3 темы, Лента', expected: 'По каждой' },
        { id: '11.8', name: 'Достижения', critical: false, preconditions: 'Условия выполнены', expected: 'Документировать' },
        { id: '11.9', name: 'Экспорт', critical: false, preconditions: 'Любой', expected: 'Документировать' },
        { id: '11.10', name: 'После сброса', critical: false, preconditions: 'Марафон сброшен', expected: 'День 1' },
      ]
    },
  ];
}
