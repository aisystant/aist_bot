# Сценарий 03-27: /onboarding_time

**Категория:** микро (вид C, несмотря на wizard — данные сохраняются одним действием)
**Что делает:** wizard backfill — записывает исторические часы саморазвития (среднее ч/нед × N недель).
**Источник в RCS:** bh.inv (Инвестиционность времени, FORM.089 §12.2).
**WP:** WP-310 Ф13c

## Поток (3 шага)

```
/onboarding_time
→ Шаг 1: «Сколько в среднем часов в неделю ты тратил на саморазвитие?»
  [2] [4] [6] [8] [10] [Свой ввод]

→ Шаг 2: «Понял: N ч/нед. За какой период?»
  [1 нед] [4 нед] [8 нед] [12 нед] [24 нед]

→ Шаг 3 (подтверждение): «Подтверди: N ч/нед × M нед = T ч
   (P ч/день — D синтетических событий). Записать?»
  [Да, записать] [Отменить]

→ После Да: INSERT D событий в прошлое (по одному на каждый день)
→ Бот: «✅ Записал T ч за M нед (D/D событий).
         Запусти /twin чтобы увидеть пересчитанную ступень.»
```

## Техническое

- Каждое событие: `occurred_at = now - timedelta(days=D-i-1)` — уникальная дата
- Функция: `db/queries/events.py::log_event_with_occurred_at()`
- Batch: `batch_id = uuid4()` в payload для группировки

## Событие в Activity Hub

| Поле | Значение |
|------|---------|
| event_type | `slot_logged` |
| payload.hours | total_hours / total_days (float) |
| payload.source | `self_report_backfill` |
| payload.confidence | `estimated` |
| payload.batch_id | UUID бэтча |
| activity_domain | `practice` |

## Нет аккаунта (user_uuid = None)

→ «Аккаунт не привязан. Сначала пройди /start.»

## Связи

- `db/queries/events.py::log_event_with_occurred_at()`
- `db/queries/identity.py::get_user_uuid()`
- FSM: `handlers/slot.py::OnboardingTimeStates`
- WP-310 Ф13c
