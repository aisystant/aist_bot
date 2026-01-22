# 04.03 Отслеживание активности

> Технический сценарий подсчёта активных дней и серий (streaks).

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Тип | Технический сценарий |
| Таблица | `activity_log` |
| Файл | `db/queries/activity.py` |

---

## 1. Таблица activity_log

### Структура

```sql
CREATE TABLE activity_log (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT,
    activity_date DATE,
    activity_type TEXT,
    mode TEXT,
    reference_id INTEGER,
    created_at TIMESTAMP DEFAULT NOW(),

    UNIQUE(chat_id, activity_date, activity_type)
)
```

### Особенности

- **UNIQUE:** Максимум одна запись типа активности в день
- **Индекс:** `(chat_id, activity_date)` для быстрых запросов

---

## 2. Типы активности

| Тип | Режим | Когда записывается |
|-----|-------|-------------------|
| `theory_answer` | marathon | Ответ на вопрос урока |
| `work_product` | marathon | Отправка рабочего продукта |
| `bonus_answer` | marathon | Ответ на бонусный вопрос |
| `feed_fixation` | feed | Сохранение фиксации |

---

## 3. Функция record_active_day()

### Сигнатура

```python
async def record_active_day(
    chat_id: int,
    activity_type: str,
    mode: str = 'marathon',
    reference_id: int = None
)
```

### Алгоритм

```
1. INSERT в activity_log
   ├─ chat_id, activity_date (сегодня), activity_type, mode
   └─ ON CONFLICT DO NOTHING (уникальность)

2. Проверка: уже был активен сегодня?
   ├─ ДА → return (ничего не меняем)
   └─ НЕТ → продолжаем

3. Расчёт streak
   ├─ last_active == вчера → streak + 1
   └─ last_active != вчера → streak = 1

4. UPDATE interns
   ├─ active_days_total + 1
   ├─ active_days_streak = new_streak
   ├─ longest_streak = max(current, new_streak)
   └─ last_active_date = today
```

---

## 4. Расчёт streak

### Логика

```python
if last_active == today - 1 день:
    # Продолжаем серию (был активен вчера)
    new_streak = active_days_streak + 1
else:
    # Серия прервалась
    new_streak = 1
```

### Правило

Серия сбрасывается в 1, если пропущен хотя бы один день.

---

## 5. Поля в таблице interns

| Поле | Тип | Описание |
|------|-----|----------|
| `active_days_total` | INTEGER | Всего активных дней |
| `active_days_streak` | INTEGER | Текущая серия |
| `longest_streak` | INTEGER | Рекорд серии |
| `last_active_date` | DATE | Последний активный день |

---

## 6. Функция get_activity_stats()

### Возвращаемые данные

```python
{
    'total': active_days_total,         # Всего дней
    'streak': active_days_streak,       # Текущая серия
    'longest_streak': longest_streak,   # Рекорд
    'last_active': last_active_date,    # Последний день
    'days_active_this_week': int,       # За 7 дней
    'recent_activity': [...]            # История недели
}
```

### Источники

- Основные счётчики: таблица `interns`
- `days_active_this_week`: COUNT из `activity_log` за 7 дней

---

## 7. Когда записывается активность

### Марафон (bot.py)

```python
# При сохранении ответа
await record_active_day(chat_id, 'theory_answer', mode='marathon')
await record_active_day(chat_id, 'work_product', mode='marathon')
await record_active_day(chat_id, 'bonus_answer', mode='marathon')
```

### Лента (feed/engine.py)

```python
# При сохранении фиксации
await record_active_day(
    chat_id=self.chat_id,
    activity_type='feed_fixation',
    mode='feed',
    reference_id=session['id']
)
```

---

## 8. Диаграмма

```
Пользователь отвечает/фиксирует
    ↓
record_active_day()
    ↓
INSERT INTO activity_log
    ↓
Уже был активен сегодня?
    ├─ ДА → return
    └─ НЕТ ─┐
            ↓
        Расчёт streak
            ├─ last_active == вчера → streak + 1
            └─ иначе → streak = 1
            ↓
        UPDATE interns
            ├─ active_days_total + 1
            ├─ active_days_streak
            ├─ longest_streak
            └─ last_active_date
```

---

## 9. Пример временной шкалы

```
Пн  → Активность → streak = 1, total = 1
Вт  → Активность → streak = 2, total = 2
Ср  → Пропуск    → (ничего)
Чт  → Активность → streak = 1, total = 3  ← Сброс!
Пт  → Активность → streak = 2, total = 4
Сб  → Активность → streak = 3, total = 5
Вс  → Активность → streak = 4, total = 6
```

---

## 10. Использование в интерфейсе

### /progress

```
📊 Прогресс: Иван

Активных дней за неделю: 5
🔥 Текущая серия: 4 дня
```

### /feed_status

```
📰 Статус Ленты

Активных дней: 42
Текущая серия: 7 🔥
```

---

## 11. Ключевые файлы

| Файл | Строки | Назначение |
|------|--------|-----------|
| `db/queries/activity.py` | 14-72 | record_active_day |
| `db/queries/activity.py` | 75-104 | get_activity_stats |
| `db/models.py` | 244-264 | Таблица activity_log |
| `bot.py` | 493 | Вызов при ответе |
| `engines/feed/engine.py` | 271-276 | Вызов при фиксации |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-01-22 | Создание документа |
