# 02.05 Сценарий напоминаний

> Полное описание процесса автоматических уведомлений.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Типы напоминаний | +1ч, +3ч |
| Планировщик | APScheduler |
| Часовой пояс | Москва (UTC+3) |
| Частота проверки | Каждую минуту |

---

## 1. Типы напоминаний

### Напоминание через 1 час (+1h)

```
⏰ *Напоминание*

День {N} марафона ждёт вас!

Всего 2 темы на сегодня: урок и задание.

/learn — начать
```

### Напоминание через 3 часа (+3h)

```
🔔 *Последнее напоминание*

День {N} ещё не начат.

Помните: *регулярность > интенсивность*.
Даже 15 минут сегодня — это прогресс.

/learn — начать
```

---

## 2. Жизненный цикл напоминания

### Диаграмма

```
┌─────────────────────────────────────────────────────┐
│ 1. Отправка темы пользователю                       │
│    send_scheduled_topic(chat_id, bot)               │
│                                                     │
│    [Новый формат WP-330 — актуально]                │
│    → scheduler рендерит get_day_text(day, 'lesson') │
│    → отправляет статический текст + кнопка практики │
│    [marathon_get_lesson кнопка-напоминание]          │
│    → try_deliver_new_marathon() → урок текущего дня │
│                                                     │
│    [USE_STATE_MACHINE=true — deprecated]            │
│    → state_machine.go_to("workshop.marathon.lesson")│
│    [USE_STATE_MACHINE=false — deprecated]           │
│    → send_theory_topic() / send_practice_topic()    │
└────────────────────┬────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────┐
│ 2. Планирование напоминаний                         │
│    schedule_reminders(chat_id, intern)              │
│                                                     │
│    DELETE старые неотправленные                     │
│    INSERT +1h (now + 1 час)                         │
│    INSERT +3h (now + 3 часа)                        │
└────────────────────┬────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────┐
│ 3. Ожидание (scheduler проверяет каждую минуту)    │
│    scheduled_check() → check_reminders()            │
└────────────────────┬────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────┐
│ 4. Время пришло: scheduled_for <= now              │
│                                                     │
│    SELECT * FROM reminders                          │
│    WHERE sent = FALSE AND scheduled_for <= now      │
└────────────────────┬────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────┐
│ 5. Проверка условий                                 │
│                                                     │
│    topics_today > 0? → НЕ отправлять               │
│    marathon_day == 0? → НЕ отправлять              │
└────────────────────┬────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────┐
│ 6. Отправка и обновление                            │
│                                                     │
│    send_reminder(chat_id, reminder_type)            │
│    UPDATE sent = TRUE                               │
└─────────────────────────────────────────────────────┘
```

---

## 3. Условия отправки

### Напоминание НЕ отправляется, если:

| Условие | Причина |
|---------|---------|
| `topics_today > 0` | Ученик уже начал обучение сегодня |
| `marathon_day == 0` | Марафон ещё не начался |

### Логика проверки

```python
async def send_reminder(chat_id, reminder_type, bot):
    intern = await get_intern(chat_id)
    topics_today = get_topics_today(intern)

    # Если уже начал — не напоминаем
    if topics_today > 0:
        return

    marathon_day = get_marathon_day(intern)
    if marathon_day == 0:
        return

    # Отправляем напоминание
    ...
```

---

## 4. Таблица reminders

### Структура

```sql
CREATE TABLE reminders (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT,
    reminder_type TEXT,        -- '+1h' или '+3h'
    scheduled_for TIMESTAMP,   -- Время отправки
    sent BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
)
```

### Примеры записей

| id | chat_id | reminder_type | scheduled_for | sent |
|----|---------|---------------|---------------|------|
| 1 | 123456 | +1h | 2026-01-22 10:00 | FALSE |
| 2 | 123456 | +3h | 2026-01-22 12:00 | FALSE |
| 3 | 789012 | +1h | 2026-01-22 15:00 | TRUE |

---

## 5. Функции

### schedule_reminders()

```python
async def schedule_reminders(chat_id: int, intern: dict):
    """Планирует напоминания для пользователя"""
    now = moscow_now()

    async with db_pool.acquire() as conn:
        # Удаляем старые неотправленные
        await conn.execute(
            'DELETE FROM reminders WHERE chat_id = $1 AND sent = FALSE',
            chat_id
        )

        # Создаём новые +1h и +3h
        for hours in [1, 3]:
            reminder_time = now + timedelta(hours=hours)
            # Убираем timezone для TIMESTAMP без timezone
            reminder_time_naive = reminder_time.replace(tzinfo=None)
            await conn.execute(
                '''INSERT INTO reminders (chat_id, reminder_type, scheduled_for)
                   VALUES ($1, $2, $3)''',
                chat_id, f'+{hours}h', reminder_time_naive
            )
```

### check_reminders()

```python
async def check_reminders():
    """Проверяет и отправляет запланированные напоминания"""
    now = moscow_now()
    now_naive = now.replace(tzinfo=None)

    async with db_pool.acquire() as conn:
        # Получаем напоминания к отправке
        rows = await conn.fetch(
            '''SELECT id, chat_id, reminder_type FROM reminders
               WHERE sent = FALSE AND scheduled_for <= $1''',
            now_naive
        )

        for row in rows:
            try:
                await send_reminder(row['chat_id'], row['reminder_type'], bot)
                await conn.execute(
                    'UPDATE reminders SET sent = TRUE WHERE id = $1',
                    row['id']
                )
            except Exception as e:
                logger.error(f"Failed to send reminder: {e}")
```

### scheduled_check()

```python
async def scheduled_check():
    """Проверка расписания каждую минуту"""
    now = moscow_now()
    time_str = f"{now.hour:02d}:{now.minute:02d}"

    # Отправка тем по расписанию
    chat_ids = await get_all_scheduled_interns(now.hour, now.minute)
    for chat_id in chat_ids:
        await send_scheduled_topic(chat_id, bot)

    # Проверка напоминаний
    await check_reminders()
```

---

## 6. Планировщик

### Инициализация

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
scheduler.add_job(scheduled_check, 'cron', minute='*')  # Каждую минуту
scheduler.start()
```

### Параметры

| Параметр | Значение |
|----------|----------|
| Библиотека | APScheduler |
| Тип | AsyncIOScheduler |
| Триггер | CronTrigger |
| Частота | Каждую минуту (`minute='*'`) |
| Timezone | Moscow (UTC+3) |

---

## 7. Логирование

```python
# Каждые 10 минут (подтверждение работы)
logger.info(f"[Scheduler] Проверка в {time_str} MSK")

# При успешной отправке
logger.info(f"Sent {reminder_type} reminder to {chat_id}")

# При ошибке
logger.error(f"Failed to send reminder to {chat_id}: {e}")
```

---

## 8. Временная шкала примера

```
09:00  Ученик получает тему (schedule_time)
       → schedule_reminders() создаёт:
         - +1h: 10:00
         - +3h: 12:00

10:00  check_reminders() находит +1h
       → topics_today == 0? ДА
       → Отправляем "⏰ Напоминание"
       → UPDATE sent = TRUE

12:00  check_reminders() находит +3h
       → topics_today == 0? ДА
       → Отправляем "🔔 Последнее напоминание"
       → UPDATE sent = TRUE

---

Альтернативный сценарий:

09:00  Ученик получает тему
10:00  Ученик проходит урок (topics_today = 1)
10:01  check_reminders() находит +1h
       → topics_today > 0? ДА
       → НЕ отправляем (ученик уже активен)

12:00  check_reminders() находит +3h
       → topics_today > 0? ДА
       → НЕ отправляем
```

---

## 9. Особенности

### Удаление старых напоминаний

При каждой отправке новой темы все **неотправленные** напоминания пользователя удаляются перед созданием новых.

### Часовой пояс

Все расчёты и хранение используют московское время (UTC+3). TIMESTAMP хранится без timezone.

### Расширяемость

Легко добавить новые типы напоминаний:
```python
for hours in [1, 3, 5, 24]:  # Добавить +5h и +24h
    ...
```

---

## 10. Ключевые файлы

| Файл | Строки | Назначение |
|------|--------|-----------|
| `core/scheduler.py` | — | Утренняя доставка (lesson_practice), кнопки-напоминания |
| `handlers/marathon.py` | — | `try_deliver_new_marathon` — on-demand by button |
| `core/marathon_content.py` | — | Статический контент (routing по профилю) |
| `db/models.py` | 162-173 | Таблица reminders |
| `states/workshops/marathon/lesson.py` | — | **Deprecated** SM стейт: урок (удалить после 2026-07-05) |
| `states/workshops/marathon/task.py` | — | **Deprecated** SM стейт: задание (удалить после 2026-07-05) |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-06-05 | WP-330 cutover: кнопка marathon_get_lesson → try_deliver_new_marathon (новый формат). SM-routing deprecated. Обновлены ключевые файлы. |
| 2026-02-05 | Добавлена документация State Machine routing в send_scheduled_topic() |
| 2026-02-05 | Обновлены номера строк в разделе «Ключевые файлы» |
| 2026-01-22 | Создание документа |
