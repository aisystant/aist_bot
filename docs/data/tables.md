# Таблицы базы данных

> Полный реестр таблиц и их полей. Источник: `db/models.py`

---

## interns (Пользователи)

> Основная таблица пользователей бота.

### Профиль

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `chat_id` | BIGINT | — | PK, Telegram chat ID |
| `name` | TEXT | `''` | Имя пользователя |
| `occupation` | TEXT | `''` | Род деятельности |
| `role` | TEXT | `''` | Роль в работе |
| `domain` | TEXT | `''` | Область деятельности |
| `interests` | TEXT | `'[]'` | Интересы (JSON массив) |
| `motivation` | TEXT | `''` | Мотивация к обучению |
| `goals` | TEXT | `''` | Цели обучения |

### Предпочтения обучения

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `language` | TEXT | `'ru'` | Язык интерфейса и контента (ru/en/es/fr) |
| `experience_level` | TEXT | `''` | Уровень опыта |
| `difficulty_preference` | TEXT | `''` | Предпочитаемая сложность |
| `learning_style` | TEXT | `''` | Стиль обучения |
| `study_duration` | INTEGER | `15` | Длительность занятия (мин) |
| `schedule_time` | TEXT | `'09:00'` | Время занятий |
| `current_problems` | TEXT | `''` | Текущие проблемы |
| `desires` | TEXT | `''` | Пожелания |
| `topic_order` | TEXT | `'default'` | Порядок тем |

### Режимы

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `mode` | TEXT | `'marathon'` | Текущий режим (marathon/feed) |
| `current_context` | TEXT | `'{}'` | Контекст текущей сессии (JSON) |

### Марафон

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `marathon_status` | TEXT | `'not_started'` | Статус Марафона |
| `marathon_start_date` | DATE | NULL | Дата начала Марафона |
| `marathon_paused_at` | DATE | NULL | Дата паузы |
| `current_topic_index` | INTEGER | `0` | Текущий индекс темы |
| `completed_topics` | TEXT | `'[]'` | Пройденные темы (JSON) |
| `topics_today` | INTEGER | `0` | Тем пройдено сегодня |
| `last_topic_date` | DATE | NULL | Дата последней темы |

### Сложность

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `complexity_level` | INTEGER | `1` | Уровень сложности (1-3) |
| `topics_at_current_complexity` | INTEGER | `0` | Тем на текущем уровне |

### Лента

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `feed_status` | TEXT | `'not_started'` | Статус Ленты |
| `feed_started_at` | DATE | NULL | Дата начала Ленты |

### Систематичность (активность)

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `active_days_total` | INTEGER | `0` | Всего активных дней |
| `active_days_streak` | INTEGER | `0` | Текущая серия |
| `longest_streak` | INTEGER | `0` | Рекорд серии |
| `last_active_date` | DATE | NULL | Последний активный день |

### Статусы и временные метки

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `onboarding_completed` | BOOLEAN | `FALSE` | Онбординг завершён |
| `created_at` | TIMESTAMP | `NOW()` | Дата регистрации |
| `updated_at` | TIMESTAMP | `NOW()` | Последнее обновление |

---

## answers (Ответы)

> Ответы пользователей на вопросы и задания.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | FK → interns |
| `mode` | TEXT | `'marathon'` | Режим (marathon/feed) |
| `topic_index` | INTEGER | — | Индекс темы |
| `topic_id` | TEXT | — | ID темы |
| `feed_session_id` | INTEGER | — | FK → feed_sessions |
| `answer_type` | TEXT | `'theory_answer'` | Тип ответа |
| `answer` | TEXT | — | Текст ответа |
| `work_product_category` | TEXT | — | Категория РП |
| `complexity_level` | INTEGER | — | Уровень сложности |
| `created_at` | TIMESTAMP | `NOW()` | Время ответа |

**Типы ответов (`answer_type`):**
| Значение | Термин онтологии | Режим |
|----------|-----------------|-------|
| `theory_answer` | Ответ на урок | Марафон |
| `work_product` | Ответ на задание (РП) | Марафон |
| `bonus_answer` | Ответ на бонус | Марафон |
| `fixation` | Фиксация | Лента |

---

## activity_log (Лог активности)

> Запись ежедневной активности для расчёта streak.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | FK → interns |
| `activity_date` | DATE | — | Дата активности |
| `activity_type` | TEXT | — | Тип активности |
| `mode` | TEXT | — | Режим (marathon/feed) |
| `reference_id` | INTEGER | — | ID связанной записи |
| `created_at` | TIMESTAMP | `NOW()` | Время записи |

**Ограничения:**
- `UNIQUE(chat_id, activity_date, activity_type)` — одна запись типа в день

**Индексы:**
- `idx_activity_date ON (chat_id, activity_date)`

**Типы активности (`activity_type`):**
| Значение | Описание | Режим |
|----------|----------|-------|
| `theory_answer` | Ответ на вопрос урока | Марафон |
| `work_product` | Отправка рабочего продукта | Марафон |
| `bonus_answer` | Ответ на бонусный вопрос | Марафон |
| `feed_fixation` | Сохранение фиксации | Лента |

---

## feed_weeks (Недели Ленты)

> Планирование недель в режиме Ленты.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | FK → interns |
| `week_number` | INTEGER | — | Номер недели |
| `week_start` | DATE | — | Дата начала недели |
| `suggested_topics` | TEXT | `'[]'` | Предложенные темы (JSON) |
| `accepted_topics` | TEXT | `'[]'` | Принятые темы (JSON) |
| `current_day` | INTEGER | `0` | Текущий день недели |
| `status` | TEXT | `'planning'` | Статус недели |
| `ended_at` | TIMESTAMP | — | Дата завершения |
| `created_at` | TIMESTAMP | `NOW()` | Дата создания |

**Статусы (`status`):**
| Значение | Описание |
|----------|----------|
| `planning` | Выбор тем |
| `active` | Активная неделя |
| `completed` | Завершена |

---

## feed_sessions (Сессии Ленты / Дайджесты)

> Дайджесты, полученные в режиме Ленты.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `week_id` | INTEGER | — | FK → feed_weeks |
| `day_number` | INTEGER | — | День недели (1-7) |
| `topic_title` | TEXT | — | Название темы |
| `content` | TEXT | `'{}'` | Текст дайджеста (JSON) |
| `session_date` | DATE | — | Дата сессии |
| `status` | TEXT | `'active'` | Статус |
| `fixation_text` | TEXT | — | Текст фиксации |
| `completed_at` | TIMESTAMP | — | Дата завершения |
| `created_at` | TIMESTAMP | `NOW()` | Дата создания |

**Статусы (`status`):**
| Значение | Описание |
|----------|----------|
| `active` | Ожидает фиксации |
| `completed` | Фиксация сохранена |

---

## reminders (Напоминания)

> Запланированные напоминания пользователям.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | FK → interns |
| `reminder_type` | TEXT | — | Тип напоминания |
| `scheduled_for` | TIMESTAMP | — | Время отправки |
| `sent` | BOOLEAN | `FALSE` | Отправлено ли |
| `created_at` | TIMESTAMP | `NOW()` | Дата создания |

---

## qa_history (История вопросов)

> Вопросы пользователей и консультации бота.

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `id` | SERIAL | — | PK |
| `chat_id` | BIGINT | — | FK → interns |
| `mode` | TEXT | — | Режим |
| `context_topic` | TEXT | — | Тема контекста |
| `question` | TEXT | — | Вопрос пользователя |
| `answer` | TEXT | — | Ответ бота (консультация) |
| `mcp_sources` | TEXT | `'[]'` | Источники MCP (JSON) |
| `created_at` | TIMESTAMP | `NOW()` | Время вопроса |

---

## Связи между таблицами

```
interns (chat_id)
    │
    ├── answers (chat_id)
    │       └── feed_session_id → feed_sessions
    │
    ├── activity_log (chat_id)
    │
    ├── feed_weeks (chat_id)
    │       └── feed_sessions (week_id)
    │
    ├── reminders (chat_id)
    │
    └── qa_history (chat_id)
```

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-01-23 | Создание документа. Полный реестр из `db/models.py` |
