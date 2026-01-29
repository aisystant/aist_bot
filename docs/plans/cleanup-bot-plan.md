# План очистки bot.py (Неделя 6)

> Детальный план по выносу кода из bot.py в модульную структуру.
> Цель: уменьшить bot.py с 3875 строк до ~1500 строк (handlers + main).

---

## Текущее состояние

### bot.py — 3875 строк

| Секция | Строки | Размер | Статус |
|--------|--------|--------|--------|
| Конфигурация | 42-73 | ~30 | Частично вынесено в `config/` |
| Константы | 74-140 | ~65 | Частично вынесено в `config/` |
| Загрузка метаданных тем | 145-212 | ~70 | Вынесено в `core/helpers.py` |
| FSM States | 214-244 | ~30 | **Оставить** |
| Middleware | 246-263 | ~20 | Вынести |
| **БД (init_db, PostgresStorage)** | 265-595 | ~330 | **ДУБЛИРУЕТ** `db/` |
| Хелперы (get_intern, etc.) | 481-695 | ~215 | **ДУБЛИРУЕТ** `db/queries/` |
| **ClaudeClient** | 698-1105 | ~410 | **ДУБЛИРУЕТ** `clients/claude.py` |
| **MCPClient** | 1109-1268 | ~160 | **ДУБЛИРУЕТ** `clients/mcp.py` |
| Структура знаний | 1270-1543 | ~270 | Частично вынесено |
| Клавиатуры (kb_*) | 1546-1666 | ~120 | Вынести |
| Handlers | 1668-3315 | ~1650 | **Оставить** (разбить позже) |
| Scheduler | 3317-3507 | ~190 | Вынести |
| Fallback handlers | 3509-3800 | ~290 | **Оставить** |
| main() | 3802-3875 | ~75 | **Оставить** |

### Модули (уже существуют)

| Модуль | Файлы | Используется в bot.py? |
|--------|-------|------------------------|
| `config/` | settings.py, __init__.py | Частично (только `ONTOLOGY_RULES`) |
| `db/` | models.py, connection.py, queries/ | **НЕТ** — bot.py использует свой код |
| `clients/` | claude.py, mcp.py | **НЕТ** — bot.py использует свои классы |
| `core/` | intent.py, helpers.py | **ДА** |
| `engines/` | shared/, feed/, marathon/ | Частично |

---

## Этапы очистки

### Этап 1: Интеграция с `clients/` (~570 строк)

**Задача:** Удалить ClaudeClient и MCPClient из bot.py, использовать модули.

#### 1.1 Сравнение API

| Метод | bot.py | clients/claude.py | Действие |
|-------|--------|-------------------|----------|
| `generate_content()` | `(topic, intern, marathon_day, mcp_client, knowledge_client)` | `(topic, intern, mcp_client, knowledge_client)` | Убрать `marathon_day` из модуля или добавить |
| `generate_question()` | `(topic, intern, marathon_day, bloom_level)` | Существует | Проверить совместимость |
| `generate_practice_intro()` | Существует | Существует | Проверить совместимость |

#### 1.2 Шаги

1. **Добавить `marathon_day` в `clients/claude.py`** если отличается
2. **Заменить импорт в bot.py:**
   ```python
   # БЫЛО:
   class ClaudeClient:
       ...
   claude = ClaudeClient()

   # СТАЛО:
   from clients import claude
   ```
3. **Заменить импорт MCPClient:**
   ```python
   # БЫЛО:
   class MCPClient:
       ...
   mcp_guides = MCPClient(MCP_URL, "MCP-Guides")
   mcp_knowledge = MCPClient(KNOWLEDGE_MCP_URL, "MCP-Knowledge", search_tool="search")

   # СТАЛО:
   from clients import mcp_guides, mcp_knowledge
   ```
4. **Удалить классы из bot.py** (строки 698-1268)
5. **Протестировать генерацию контента**

**Экономия:** ~570 строк

---

### Этап 2: Интеграция с `db/` (~330 строк)

**Задача:** Удалить дублирующий код БД из bot.py.

#### 2.1 Что дублируется

| Функция в bot.py | Эквивалент в db/ | Строки в bot.py |
|------------------|------------------|-----------------|
| `init_db()` | `db.init_db()` | 269-424 |
| `PostgresStorage` | Не нужен (aiogram использует) | 426-479 |
| `get_intern()` | `db.queries.users.get_intern()` | 481-550 |
| `update_intern()` | `db.queries.users.update_intern()` | 552-561 |
| `save_answer()` | `db.queries.answers.save_answer()` | 563-585 |
| `get_all_scheduled_interns()` | Нужно добавить в db/queries | 587-595 |

#### 2.2 Шаги

1. **Проверить совместимость** `db/queries/users.py` с bot.py
2. **Добавить недостающие функции** в `db/queries/`
3. **Заменить импорты:**
   ```python
   # БЫЛО:
   db_pool = None
   async def init_db(): ...

   # СТАЛО:
   from db import init_db, db_pool
   from db.queries.users import get_intern, update_intern
   from db.queries.answers import save_answer
   ```
4. **Удалить дублирующий код** (строки 265-595)
5. **PostgresStorage** — проверить, используется ли. Если да — вынести в `db/storage.py`

**Экономия:** ~330 строк

---

### Этап 3: Вынос клавиатур (~120 строк)

**Задача:** Вынести все `kb_*` функции в отдельный модуль.

#### 3.1 Функции для выноса

```
kb_experience()
kb_difficulty()
kb_learning_style()
kb_study_duration()
kb_confirm()
kb_learn()
kb_update_profile()
kb_bloom_level()
kb_bonus_question()
kb_skip_topic()
kb_marathon_start()
kb_submit_work_product()
kb_language_select()
progress_bar()
```

#### 3.2 Шаги

1. **Создать** `keyboards.py` или `ui/keyboards.py`
2. **Перенести все kb_* функции**
3. **Обновить импорты в bot.py**

**Экономия:** ~120 строк

---

### Этап 4: Вынос scheduler (~190 строк)

**Задача:** Вынести планировщик в отдельный модуль.

#### 4.1 Функции для выноса

```
send_scheduled_topic()
schedule_reminders()
send_reminder()
check_reminders()
scheduled_check()
```

#### 4.2 Шаги

1. **Создать** `scheduler/` или `engines/scheduler.py`
2. **Перенести функции**
3. **Обновить bot.py** — оставить только инициализацию scheduler

**Экономия:** ~190 строк

---

### Этап 5: Консолидация config (~65 строк)

**Задача:** Убедиться, что все константы используются из `config/`.

#### 5.1 Константы в bot.py

```python
DIFFICULTY_LEVELS = {...}
LEARNING_STYLES = {...}
EXPERIENCE_LEVELS = {...}
STUDY_DURATIONS = {...}
```

#### 5.2 Шаги

1. **Проверить** что константы есть в `config/settings.py`
2. **Заменить на импорты** из `config`
3. **Удалить дублирование**

**Экономия:** ~65 строк

---

## Итоговая экономия

| Этап | Строки |
|------|--------|
| Этап 1: clients/ | ~570 |
| Этап 2: db/ | ~330 |
| Этап 3: keyboards | ~120 |
| Этап 4: scheduler | ~190 |
| Этап 5: config | ~65 |
| **ИТОГО** | **~1275** |

**Результат:** bot.py уменьшится с 3875 до ~2600 строк.

---

## Что остается в bot.py

После очистки bot.py должен содержать только:

1. **Импорты** из модулей
2. **FSM States** (OnboardingStates, LearningStates, UpdateStates)
3. **Router и Handlers** (~1650 строк)
4. **Fallback handlers** (~290 строк)
5. **main()** и инициализация (~75 строк)

---

## Порядок выполнения

```
1. [X] Анализ дублирования
2. [ ] Этап 1: Интеграция clients/
   2.1. [ ] Сравнить API ClaudeClient
   2.2. [ ] Обновить clients/claude.py если нужно
   2.3. [ ] Заменить импорты
   2.4. [ ] Удалить классы из bot.py
   2.5. [ ] Протестировать
3. [ ] Этап 2: Интеграция db/
   3.1. [ ] Сравнить API db/queries
   3.2. [ ] Добавить недостающие функции
   3.3. [ ] Заменить импорты
   3.4. [ ] Удалить код из bot.py
   3.5. [ ] Протестировать
4. [ ] Этап 3: Вынос клавиатур
5. [ ] Этап 4: Вынос scheduler
6. [ ] Этап 5: Консолидация config
7. [ ] Финальное тестирование
8. [ ] Коммит
```

---

## Критерии готовности

- [ ] bot.py < 2700 строк
- [ ] Нет дублирования классов (ClaudeClient, MCPClient)
- [ ] Все константы из config/
- [ ] Все DB-операции через db/
- [ ] Бот запускается без ошибок: `python -c "from bot import dp, bot"`
- [ ] Тесты проходят

---

## Связь с State Machine миграцией

Эта очистка — **подготовительный этап** перед миграцией на State Machine:

1. **До очистки:** bot.py = монолит 3875 строк, сложно мигрировать
2. **После очистки:** bot.py = handlers + main, легко разбить на states/

Последовательность:
```
Неделя 6: Очистка bot.py (этот план)
    ↓
Неделя 7: State Machine инфраструктура (Week 1 из MIGRATION_PLAN_V2)
    ↓
Неделя 8+: Миграция handlers → states
```

---

## Дополнительные требования (из отчёта о прогрессе)

### Изоляция состояний

> "Каждое состояние вынесено в отдельный файл для упрощения тестирования и поддержки"

После очистки bot.py, handlers должны быть готовы к разбиению по файлам:

```
handlers/
├── __init__.py
├── onboarding.py      # cmd_start, on_name, on_occupation, etc.
├── learning.py        # cmd_learn, on_answer, on_bonus_*
├── profile.py         # cmd_profile, cmd_update, on_upd_*
├── progress.py        # cmd_progress, show_full_progress
├── feed.py           # (уже в engines/feed/handlers.py)
└── fallback.py       # on_unknown_message, on_unknown_callback
```

### Локализация (i18n)

> "Все захардкоженные строки переведены на i18n"

При очистке:
1. Выявить захардкоженные строки в bot.py
2. Перенести в `locales.py` или будущую `i18n/` систему
3. Использовать `t()` функцию

### Compliance Checker

> "Автоматическая проверка соответствия кода документации"

После очистки:
- Обновить `tests/test_repo/requirements-*.yaml`
- Добавить проверки для новых модулей
- Убедиться, что документация соответствует коду

### ORY интеграция (будущее)

Подготовить код к интеграции с ORY:
- Абстрагировать работу с user_id
- Не хардкодить telegram_id как единственный идентификатор
- Оставить место для расширения модели пользователя

### Режимы консультанта (будущее)

> "Режимы консультанта (эксперт, Стратег, Артефактор) в виде независимых модулей"

Архитектура после очистки должна поддерживать:
```
engines/
├── consultants/
│   ├── base.py         # Базовый консультант
│   ├── expert.py       # Эксперт
│   ├── strategist.py   # Стратег
│   └── artificer.py    # Артефактор
```

---

## Расширенный чеклист

### Перед началом очистки
- [ ] Создать бэкап bot.py
- [ ] Убедиться, что тесты проходят
- [ ] Зафиксировать текущий размер файла (3875 строк)

### В процессе очистки
- [ ] После каждого этапа — smoke test: `python -c "from bot import dp, bot"`
- [ ] Документировать изменения в git commit messages
- [ ] Обновлять compliance reports

### После очистки
- [ ] bot.py < 2700 строк
- [ ] Все тесты проходят
- [ ] Compliance checker зелёный
- [ ] README.md обновлён (если нужно)
- [ ] Готовность к State Machine миграции подтверждена
