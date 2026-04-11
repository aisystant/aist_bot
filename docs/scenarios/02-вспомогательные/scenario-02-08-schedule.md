# 02.08 `/schedule` — каталог курсов по расписанию

> Hub-навигация по курсам Aisystant: категории → курсы → страница курса → оплата.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/schedule` |
| Вид | Вспомогательная (B) — inline callback navigation |
| Файл | [`handlers/schedule.py:182`](../../../handlers/schedule.py) |
| FSM | Нет (inline callbacks, несколько экранов) |
| Platform | Aisystant |

---

## 1. Различение `/schedule` vs `/buy`

- **`/buy`** — прямой storefront: «что купить сейчас»
- **`/schedule`** — навигационный hub: «что вообще есть в каталоге, с фильтрами по категориям и датам»

Оба в итоге приводят к Yookassa payment URL, но пути разные.

## 2. Flow

```
/schedule
  ↓
Hub (категории курсов)
  ↓
Category view (список курсов в категории)
  ↓
Course page
  ├─ Описание, даты, цена
  ├─ [Купить полностью] → Yookassa
  └─ [Купить в рассрочку] → Yookassa split
```

## 3. Источники

| Что | Откуда |
|-----|--------|
| Категории | `clients/aisystant.get_course_categories()` |
| Курсы | `clients/aisystant.get_available_courses()` |
| User lessons | `clients/aisystant.get_user_lessons(aisystant_id)` — какие курсы уже куплены |
| Payment | Yookassa |

## 4. Not scheduling time

⚠️ **Не путать:** `/schedule` в боте Aisystant — про **каталог курсов**, не про время ежедневного занятия. Время занятия (ежедневное расписание марафона/ленты) настраивается через **/profile** или в онбординге (`schedule_time` field в `development.user_state`).

## 5. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/schedule.py` | `cmd_schedule` + callbacks |
| `clients/aisystant.py` | LMS API |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
