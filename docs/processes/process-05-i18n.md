# P-05: Процесс локализации (i18n)

> Описывает архитектуру и процессы работы с многоязычностью бота.

---

## 1. Архитектура

### 1.1. Структура файлов

```
i18n/
├── __init__.py           # Экспорт публичного API: t(), detect_language()
├── loader.py             # Загрузка и кэширование переводов
├── checker.py            # CLI для проверки и экспорта переводов
├── schema.yaml           # МАСТЕР-ФАЙЛ: все ключи + RU + EN + метаданные
└── translations/
    ├── es.yaml           # Испанский перевод
    ├── fr.yaml           # Французский (пример)
    └── <lang>.yaml       # Другие языки
```

### 1.2. Принцип работы

```
schema.yaml (источник истины)
    │
    ├── RU: русский текст (обязательно)
    ├── EN: английский текст (обязательно)
    └── description: контекст для переводчика (опционально)

translations/<lang>.yaml
    └── Только переводы, без метаданных
```

### 1.3. Fallback-цепочка

```
Запрошенный язык (es) → Английский (en) → Русский (ru) → Ключ
```

---

## 2. Использование в коде

### 2.1. Импорт

```python
from i18n import t, detect_language, get_language_name, SUPPORTED_LANGUAGES
```

### 2.2. Получение перевода

```python
# Простой текст
message = t('welcome.greeting', lang)

# С плейсхолдерами
message = t('progress.day', lang, day=5, total=14)

# Fallback на русский если ключ не найден
message = t('unknown.key', 'es')  # Вернёт русский или ключ
```

### 2.3. Определение языка пользователя

```python
# Из Telegram language_code
lang = detect_language(user.language_code)  # 'ru', 'en', 'es'

# Из профиля пользователя
lang = intern.get('language', 'ru') or 'ru'
```

### 2.4. Поддерживаемые языки

```python
SUPPORTED_LANGUAGES = ['ru', 'en', 'es']
```

---

## 3. Добавление нового языка

### 3.1. Шаги

1. **Создать файл перевода**
   ```bash
   # Экспорт шаблона
   python -m i18n.checker export --lang fr --format yaml > i18n/translations/fr.yaml
   ```

2. **Перевести все ключи**
   - Открыть `fr.yaml`
   - Заполнить переводы
   - Сохранить плейсхолдеры `{name}`, `{day}` и т.д.

3. **Добавить язык в код**
   ```python
   # i18n/loader.py
   SUPPORTED_LANGUAGES = ['ru', 'en', 'es', 'fr']  # Добавить 'fr'

   # Также в detect_language() и get_language_name()
   ```

4. **Проверить полноту**
   ```bash
   python -m i18n.checker check --lang fr
   ```

5. **Добавить в UI выбора языка**
   - `bot.py`: функция `kb_languages()`
   - `engines/mode_selector.py`: если есть выбор языка

### 3.2. Полный чеклист нового языка (13 мест!)

> ⚠️ Пропуск любого пункта приведёт к частичной локализации!

#### Регистрация языка
- [ ] `i18n/loader.py:31` — добавить в `SUPPORTED_LANGUAGES`
- [ ] `i18n/loader.py:37` — добавить в `get_language_name()`

#### UI переводы
- [ ] `i18n/translations/{lang}.yaml` — создать файл со всеми переводами

#### Названия тем Марафона
- [ ] `knowledge_structure.yaml` — добавить `title_{lang}` для всех **28 тем**

#### Инструкции для генерации контента (11 мест!)
- [ ] `bot.py` — 3 словаря `lang_instruction` (~840, ~940, ~1060)
- [ ] `clients/claude.py` — 3 словаря `lang_instruction` (~150, ~250, ~350)
- [ ] `engines/feed/planner.py` — 3 словаря `lang_instruction` (~45, ~395, ~545)
- [ ] `engines/shared/question_handler.py` — 2 словаря `lang_instruction` (~290, ~405)

#### Fallback темы Ленты
- [ ] `engines/feed/planner.py:192-304` — добавить `fallback_topics['{lang}']` (5 тем)

#### UI элементы
- [ ] `bot.py:1624` — обновить текст кнопки "Language (en, es, fr, ru)"

#### State Machine метаданные
- [ ] `states/base.py` — display_name (пример)
- [ ] `states/common/start.py` — display_name
- [ ] `states/common/error.py` — display_name + RETRY_BUTTONS, BACK_BUTTONS
- [ ] `states/common/mode_select.py` — display_name + MARATHON_BUTTONS, FEED_BUTTONS, SETTINGS_BUTTONS
- [ ] `states/common/consultation.py` — display_name
- [ ] `states/feed/digest.py` — display_name
- [ ] `states/feed/topics.py` — display_name
- [ ] `states/workshops/marathon/lesson.py` — display_name
- [ ] `states/workshops/marathon/question.py` — display_name
- [ ] `states/workshops/marathon/bonus.py` — display_name + YES_BUTTONS, NO_BUTTONS
- [ ] `states/workshops/marathon/task.py` — display_name

#### Тесты
- [ ] `tests/i18n/test_translations.py`:
  - `test_{lang}_completeness`
  - `test_placeholders_preserved_in_{lang}`
  - `test_critical_key_exists_in_{lang}`
  - Обновить `test_basic_translation`, `test_stats_method`, `test_detect_supported_languages`

#### Документация
- [ ] `i18n/loader.py:220` — обновить docstring функции `t()`
- [ ] `docs/data/tables.md` — обновить список языков в поле `language`

### 3.3. Типичные ошибки при добавлении языка

| Симптом | Причина | Решение |
|---------|---------|---------|
| UI на новом языке, контент на русском | Пропущен `lang_instruction` | Добавить во все 11 мест |
| Название темы на русском | Пропущен `title_{lang}` | Добавить в `knowledge_structure.yaml` |
| Язык не сохраняется | Нет поля в БД | Проверить `language` в `db/models.py` |
| Тесты падают | Пропущены тесты | Обновить `test_translations.py` |

---

## 4. Добавление новых ключей

### 4.1. Где добавлять

**Всегда в `schema.yaml`**, не в отдельные переводы!

### 4.2. Формат ключа

```yaml
section:
  key_name:
    ru: "Русский текст"
    en: "English text"
    description: "Контекст для переводчика (опционально)"
    max_length: 20  # Для кнопок (опционально)
```

### 4.3. Правила именования

| Секция | Для чего |
|--------|----------|
| `welcome` | Приветствие, /start |
| `commands` | Описания команд |
| `menu` | Короткие названия для меню Telegram |
| `onboarding` | Онбординг |
| `buttons` | Кнопки (короткие!) |
| `progress` | /progress команда |
| `modes` | /mode, настройки режимов |
| `marathon` | Марафон: уроки, задания |
| `feed` | Лента: дайджесты, темы |
| `profile` | /profile |
| `update` | /update профиля |
| `help` | /help справка |
| `errors` | Сообщения об ошибках |
| `fsm` | FSM fallback-сообщения |
| `loading` | Индикаторы загрузки |
| `shared` | Общие слова: "из", "и", предлоги |

### 4.4. Чеклист нового ключа

- [ ] Ключ добавлен в `schema.yaml` с RU и EN
- [ ] Код использует `t('section.key', lang)`
- [ ] Перевод добавлен в `es.yaml` (и другие языки)
- [ ] Проверка: `python -m i18n.checker check`

---

## 5. CLI-инструменты

### 5.1. Проверка полноты

```bash
# Все языки
python -m i18n.checker check

# Конкретный язык
python -m i18n.checker check --lang es

# Вывод
# ✅ es: 245/250 ключей (98%)
# ❌ Отсутствуют: progress.work_products, modes.complexity_title
```

### 5.2. Экспорт для переводчика

```bash
# YAML (для разработчика)
python -m i18n.checker export --lang fr --format yaml

# CSV (для переводчика)
python -m i18n.checker export --lang fr --format csv

# JSON
python -m i18n.checker export --lang fr --format json
```

### 5.3. Импорт перевода

```bash
python -m i18n.checker import --file fr_translations.csv --lang fr
```

---

## 6. Частые ошибки

### 6.1. Захардкоженные строки

❌ **Неправильно:**
```python
await message.answer("Привет!")
await message.answer(f"День {day} из 14")
```

✅ **Правильно:**
```python
await message.answer(t('welcome.greeting', lang))
await message.answer(t('progress.day', lang, day=day, total=14))
```

### 6.2. Забытый язык

❌ **Неправильно:**
```python
await message.answer(t('welcome.greeting'))  # Нет lang!
```

✅ **Правильно:**
```python
lang = intern.get('language', 'ru') or 'ru'
await message.answer(t('welcome.greeting', lang))
```

### 6.3. Несуществующий ключ

```python
# Если ключа нет — вернётся сам ключ
t('typo.keyy', lang)  # Вернёт "typo.keyy"
```

### 6.4. Потерянные плейсхолдеры

❌ **В переводе:**
```yaml
# schema.yaml
progress:
  day:
    ru: "День {day} из {total}"
    en: "Day {day} of {total}"

# es.yaml — ОШИБКА! Потеряны плейсхолдеры
progress:
  day: "Día del maratón"  # Где {day} и {total}?!
```

✅ **Правильно:**
```yaml
progress:
  day: "Día {day} de {total}"
```

---

## 7. Интеграция с кодовой базой

### 7.1. Ключевые файлы

| Файл | Что локализовать |
|------|------------------|
| `bot.py` | Все `message.answer()`, `edit_text()` |
| `engines/mode_selector.py` | Режимы, настройки |
| `engines/feed/handlers.py` | Лента, дайджесты |
| `scheduler.py` | Напоминания |

### 7.2. Как найти захардкоженные строки

```bash
# Поиск русских строк в await message.answer()
grep -rn "await.*answer.*[А-Яа-я]" bot.py engines/

# Поиск текста кнопок
grep -rn 'text=.*"[А-Яа-я]' bot.py engines/
```

---

## 8. Связи с другими процессами

| Процесс | Связь |
|---------|-------|
| P-01 Activity | Локализация сообщений об активности |
| P-02 Content | Генерация контента на языке пользователя |
| P-04 Stats | Локализация статистики |

---

## 9. Версионирование

- При добавлении нового языка — создать PR
- При добавлении >10 ключей — обновить этот документ
- Перед релизом — `python -m i18n.checker check`
