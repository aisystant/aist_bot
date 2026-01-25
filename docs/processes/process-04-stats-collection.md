# P-04 Сбор статистики

> Процесс агрегации данных для команды /progress и других отчётов.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Тип | Процесс |
| Файлы | `db/queries/answers.py`, `db/queries/activity.py` |
| Использует | `/progress`, `/profile` |

---

## 1. Источники данных

| Метрика | Таблица | Запрос |
|---------|---------|--------|
| Активные дни (неделя) | `activity_log` | `COUNT DISTINCT activity_date WHERE date > NOW() - 7 days` |
| Активные дни (всего) | `interns` | `active_days_total` |
| Текущая серия | `interns` | `active_days_streak` |
| Пройдено тем | `interns` | `completed_topics` |
| Рабочие продукты | `answers` | `COUNT WHERE answer_type = 'work_product'` |
| Дайджесты | `feed_sessions` | `COUNT WHERE status = 'completed'` |
| Фиксации | `answers` | `COUNT WHERE answer_type = 'fixation'` |

---

## 2. Функции сбора

### get_activity_stats()

**Файл:** `db/queries/activity.py`

**Возвращает:**
```python
{
    'total': int,           # Всего активных дней
    'streak': int,          # Текущая серия
    'longest_streak': int,  # Рекорд серии
    'last_active': date,    # Последний активный день
    'days_active_this_week': int  # За 7 дней
}
```

### get_weekly_marathon_stats()

**Файл:** `db/queries/answers.py`

**Возвращает:**
```python
{
    'topics_completed': int,
    'work_products': int,
    'bonus_answers': int
}
```

### get_weekly_feed_stats()

**Файл:** `db/queries/feed.py`

**Возвращает:**
```python
{
    'digests_count': int,
    'fixations_count': int,
    'topics': list[str]
}
```

---

### get_days_progress()

**Файл:** `bot.py`

**Описание:** Вычисляет прогресс по каждому дню марафона.

**Возвращает:**
```python
[
    {'day': 1, 'total': 2, 'completed': 2, 'status': 'completed'},
    {'day': 2, 'total': 2, 'completed': 1, 'status': 'in_progress'},
    {'day': 3, 'total': 2, 'completed': 0, 'status': 'available'},
    ...
]
```

**Статусы:**
- `completed` — все темы дня пройдены
- `in_progress` — часть тем пройдена
- `available` — день доступен, темы не начаты
- `locked` — день ещё не наступил

---

## 3. Расчёт отставания

**Формула:**
```python
completed_days = sum(1 for d in days_progress if d['status'] == 'completed')
lag = marathon_day - completed_days
```

**Логика:**
- `marathon_day` — текущий календарный день марафона (1-14)
- `completed_days` — количество дней со статусом `completed`
- `lag` — разница показывает, сколько дней контента пользователь пропустил

**Пример:**
| День | Статус | Учитывается |
|------|--------|-------------|
| 1 | completed | ✓ |
| 2 | completed | ✓ |
| 3 | completed | ✓ |
| 4 | in_progress | ✗ |
| 5 | available | ✗ |
| 6 | available | ✗ |
| 7 | available | ✗ |

- `marathon_day = 7`
- `completed_days = 3`
- `lag = 7 - 3 = 4 дня`

---

## 4. Диаграмма

```
/progress
    ↓
cmd_progress()
    ├─ get_activity_stats()           → activity_log, interns
    ├─ get_weekly_marathon_stats()    → answers
    └─ get_weekly_feed_stats()        → feed_sessions, answers
    ↓
Формирование отчёта
    ↓
Отправка пользователю
```

---

## 5. Связь с другими процессами

- **P-01 Отслеживание активности** — записывает данные, которые потом агрегируются здесь
- **Сценарий 03.01 Прогресс** — использует этот процесс для отображения UI

---

## 6. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `db/queries/activity.py` | Статистика активности |
| `db/queries/answers.py` | Статистика ответов |
| `bot.py:cmd_progress` | Команда /progress |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-01-23 | Создание документа |
| 2026-01-25 | Добавлен раздел 3 «Расчёт отставания» и функция get_days_progress() |
