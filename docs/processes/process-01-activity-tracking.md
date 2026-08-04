# P-01 Отслеживание активности

> Процесс записи активных дней и расчёта серий (streaks).

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Тип | Процесс |
| Таблица | `activity_log` (learning БД), `development.user_state`, `development.daily_activity_marker` (обе — dev БД) |
| Файл | `db/queries/activity.py` |

> **Миграция WP-82 Ф3 (2026-03-17):** таблица `interns` удалена, счётчики живут в `development.user_state` (см. `docs/data/tables.md`). Этот документ обновлён под текущую схему WP-7 Ф48 (2026-08-04) — старые упоминания `interns` ниже были стале.

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
| `feed_fixation` | feed | Сохранение фиксации (два независимых движка ленты) |
| `question_asked` | — | Заданный вопрос |
| `marathon_checkin` | marathon | `handlers/marathon.py` |

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

### Алгоритм (WP-7 Ф48, 2026-08-04)

```
1. INSERT в activity_log (learning БД) — аналитика/аудит, НЕ гейт счётчика
   ├─ chat_id, activity_date (сегодня), activity_type, mode
   └─ ON CONFLICT DO NOTHING (уникальность по типу активности)

2. Атомарный гейт + обновление счётчиков — одна транзакция dev БД:
   a. SELECT ... FOR UPDATE строки development.user_state (лочит первой)
   b. INSERT INTO daily_activity_marker (chat_id, activity_date)
      ON CONFLICT DO NOTHING RETURNING 1
      ├─ конфликт (уже был маркер сегодня) → return, счётчики не трогаем
      └─ вставился → продолжаем
   c. compute_streak_update(): новый streak по наличию маркера ЗА ВЧЕРА
   d. UPDATE user_state: total+1, streak, longest_streak
```

**Почему не через `last_active_date`.** До WP-7 Ф48 шаг 2 гейтился через
`last_active_date < today`. Эта же колонка отдельно обновляется
`touch_last_active_date()` (fire-and-forget на КАЖДОЕ взаимодействие,
core/middleware.py) — гонка двух независимых writer'ов молча теряла инкремент.
`daily_activity_marker` — отдельная таблица-гейт в той же БД, что и счётчики,
поэтому маркер и счётчик обновляются в одной транзакции. `last_active_date`
продолжает означать «дата любого взаимодействия» — нужна DAU/WAU/MAU,
nudges, разблокировке `bot_blocked`; в счётчике активных дней не участвует.

---

## 4. Расчёт streak

### Логика (`compute_streak_update()`, db/queries/activity.py)

```python
def compute_streak_update(current_streak, had_yesterday, current_longest):
    continues = current_streak > 0 and had_yesterday
    new_streak = current_streak + 1 if continues else 1
    new_longest = max(current_longest or 0, new_streak)
    return new_streak, new_longest
```

`had_yesterday` — наличие строки в `daily_activity_marker` за вчера, не
`last_active_date`. Условие `current_streak > 0` обязательно: без него
пользователь сразу после `/mydata` сброса статистики (streak=0), у которого
вчерашний маркер ещё не удалён гонкой с `record_active_day`, получил бы
продолжение уже сброшенной серии (найдено в code review, peer-session
2026-08-04-27-wp7-f48-bot-reliability).

### Правило

Серия сбрасывается в 1, если пропущен хотя бы один день или пользователь
сбросил статистику (`/mydata` → «Сбросить»).

---

## 5. Поля в development.user_state (систематичность)

| Поле | Тип | Описание |
|------|-----|----------|
| `active_days_total` | INTEGER | Всего активных дней |
| `active_days_streak` | INTEGER | Текущая серия |
| `longest_streak` | INTEGER | Рекорд серии |
| `last_active_date` | DATE | Дата ЛЮБОГО взаимодействия (не гейт счётчика — см. §3) |
| `stats_reset_date` | DATE | Когда пользователь последний раз сбросил статистику (`/progress` → `reset_user_stats()`, или `/mydata` → `_reset_stats()`, обе ветки пишут это поле с WP-7 Ф48) |

**Отдельная таблица `development.daily_activity_marker`** (`chat_id`, `activity_date`,
`PRIMARY KEY(chat_id, activity_date)`) — служебный гейт для §3, наружу не читается.

---

## 6. Функция get_activity_stats()

### Возвращаемые данные

```python
{
    'total': active_days_total,         # Всего дней
    'streak': active_days_streak,       # Текущая серия
    'longest_streak': longest_streak,   # Рекорд
    'last_active': last_active_date,    # Последний день
    'days_active_this_week': int,       # С понедельника текущей недели
    'recent_activity': [...]            # История недели
}
```

### Источники

- Основные счётчики: `development.user_state`
- `days_active_this_week`: COUNT из `activity_log` с понедельника текущей недели

---

## 7. Когда записывается активность

### Марафон (core/topics.py, handlers/marathon.py)

```python
await record_active_day(chat_id, 'theory_answer', mode='marathon')
await record_active_day(chat_id, 'work_product', mode='marathon')
await record_active_day(chat_id, 'bonus_answer', mode='marathon')
await record_active_day(chat_id, 'marathon_checkin', mode='marathon')
```

### Лента (два независимых движка)

```python
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
INSERT INTO activity_log (аналитика, learning БД)
    ↓
dev БД, одна транзакция:
    SELECT user_state FOR UPDATE
    ↓
    INSERT daily_activity_marker ON CONFLICT DO NOTHING
    ├─ конфликт (уже сегодня) → return
    └─ вставился ─┐
                  ↓
            compute_streak_update(streak, had_yesterday, longest)
                  ↓
            UPDATE user_state
                ├─ active_days_total + 1
                ├─ active_days_streak
                └─ longest_streak
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
| `db/queries/activity.py` | 16-26 | compute_streak_update() — чистая функция, юнит-тест |
| `db/queries/activity.py` | 56-138 | record_active_day() |
| `db/queries/activity.py` | 151-181 | get_activity_stats() |
| `db/models.py` | 187-194 | Таблица development.daily_activity_marker |
| `db/models.py` | 475+ | Таблица activity_log |
| `db/migrations/039_wp7_f48_daily_activity_marker.py` | — | Ручной прогон на существующей БД |
| `scripts/wp7_f48_dry_run_activity_delta.py` | — | Read-only отчёт по дельтам счётчик↔журнал |
| `tests/test_wp7_f48_activity_streak.py` | — | Регрессия на compute_streak_update() |
| `states/utilities/mydata.py` | `_reset_stats()` | Сброс через `/mydata` — пишет stats_reset_date, чистит daily_activity_marker |
| `db/queries/answers.py` | `reset_user_stats()` | Сброс через `/progress` — независимая от `/mydata` ветка, сохраняет active_days_total (см. §12) |
| `bot.py` | 485-507 | save_answer() — вызывает record_active_day (номера строк не сверялись в этой сессии) |
| `engines/feed/engine.py` | 271-276 | Вызов при фиксации (номера строк не сверялись в этой сессии) |

---

## 12. Известный разрыв — два независимых сброса статистики (частично исправлено, WP-7 Ф48)

Обнаружено 2026-08-04 при работе над гонкой счётчика (peer-session
2026-08-04-27-wp7-f48-bot-reliability). Обе функции теперь корректно
взаимодействуют с гейтом `daily_activity_marker` (см. §3-5) — это починено
в этой сессии (изначально было починено только для `/mydata`, cold-review
в этой же сессии нашёл ту же дыру во второй ветке и она была закрыта
симметрично). Оставшийся разрыв — продуктовый, не инженерный, зафиксирован
здесь для решения пилотом отдельно.

`/progress` → «Сбросить статистику» и `/mydata` → «Сбросить» — два разных
callback'а на два разных запроса, с расходящимся поведением:

| | `reset_user_stats()` (db/queries/answers.py, вызывается из /progress) | `_reset_stats()` (states/utilities/mydata.py, вызывается из /mydata) |
|---|---|---|
| `active_days_total` | **сохраняет** | обнуляет |
| `active_days_streak`, `longest_streak`, `last_active_date` | обнуляет | обнуляет |
| `stats_reset_date` | пишет | пишет (с WP-7 Ф48) |
| `daily_activity_marker` | чистит (с WP-7 Ф48) | чистит (с WP-7 Ф48) |
| Текст пользователю | `progress.stats_reset_*` — не обещает сброс total | `mydata.stats_reset_*` (`i18n/schema.yaml`) — прямо обещает «Будут обнулены: активные дни, текущая серия, лучшая серия» |

Каждая ветка сама по себе соответствует своему тексту — разрыв в том, что
это один пользовательский смысл («сбросить статистику»), доступный двумя
путями с разным результатом (`active_days_total` — сохранить или обнулить).
Не унифицировано в этой сессии: смена поведения — продуктовый вопрос (что
должен обещать сброс), не инженерный.

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-08-04 | WP-7 Ф48: атомарный гейт daily_activity_marker вместо last_active_date (гонка с touch_last_active_date), compute_streak_update() вынесена в чистую функцию, исправлен список call sites (marathon_checkin, второй feed_fixation), задокументирован разрыв двух независимых сбросов статистики (§12, обе ветки теперь чистят маркер — вторая дыра найдена cold-review в этой же сессии), актуализированы упоминания `interns` → `development.user_state` (устарели с WP-82 Ф3, 2026-03-17) |
| 2026-01-29 | Обновлены номера строк после рефакторинга bot.py |
| 2026-01-22 | Создание документа |
| 2026-02-03 | Исправлен расчёт `days_active_this_week`: теперь с понедельника текущей недели, а не за последние 7 дней |
