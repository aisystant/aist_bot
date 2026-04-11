# 02.12 `/feedback` — отправка обратной связи

> 4-шаговый FSM wizard для отправки bug-reports / feature requests / прочего. Сохранение в `feedback_reports`.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/feedback` |
| Вид | Вспомогательная (B) — FSM wizard |
| Файл | [`handlers/feedback.py:80`](../../../handlers/feedback.py) |
| FSM state | `FeedbackState` в `states/utilities/feedback.py` |
| Таблица | `feedback_reports` |
| Quick shortcut | `!<text>` — быстрая отправка без wizard (см. §3) |

---

## 1. Flow (4 шага)

```
/feedback
  ↓
Step 1: Category
  ├─ [Bug report]      → step 2
  ├─ [Feature request] → step 3 (скип scenario)
  └─ [Other]           → step 3
  ↓
Step 2: Scenario (только для bug)
  ├─ Марафон / Лента / Тренировка / Онбординг /
  ├─ Консультация / Интеграция / Прочее
  ↓
Step 3: Severity
  ├─ 🔴 Critical — сломано
  ├─ 🟡 Yellow    — мешает, но работает
  └─ 🟢 Green     — мелочь
  ↓
Step 4: Text input (free text)
  └─ Ожидание текстового сообщения
  ↓
SAVE → feedback_reports INSERT → Confirmation
```

## 2. Step tracking

Текущий step хранится в `development.user_state.current_context.feedback_step` (JSONB, не `fsm_states.data` — §10.35). При каждом callback / text handler читает `feedback_step` и инкрементирует.

**Выход на середине:** если user вводит `/start`, `/learn` и т.д. — FSM сбрасывается, feedback теряется. Это осознанный trade-off (feedback не критичен, можно ввести заново).

## 3. Quick shortcut: `!text`

Если user пишет сообщение, начинающееся с `!` (например `!бот не отвечает на /start`), middleware перехватывает → сохраняет напрямую в `feedback_reports` с:
- `category='bug'`
- `scenario='other'`
- `severity='yellow'`
- `message=<text>`

Минует весь wizard. Используется для срочных сигналов.

## 4. Data mapping

Шаг → поле в `feedback_reports` (см. [tables.md §5.4](../../data/tables.md)):

| Step | Поле | Значения |
|------|------|----------|
| 1 | `category` | `bug` / `feature` / `other` |
| 2 | `scenario` | `marathon` / `feed` / `training` / `onboarding` / `consultation` / `integration` / `other` |
| 3 | `severity` | `red` / `yellow` / `green` |
| 4 | `message` | Free text |

После сохранения → статус `'new'` → триаж через [`/reports`](../../admin-commands.md#1-reports--триаж-пользовательских-bug-reports) admin-команду.

## 5. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/feedback.py` | `cmd_feedback` + quick shortcut middleware |
| `states/utilities/feedback.py` | FSM state (phase dispatch) |
| `db/queries/feedback.py` | `save_report()`, `clear_all_reports()` |

## 6. Связанное

- [admin /reports](../../admin-commands.md) — триаж feedback
- [P-06 Observability](../../processes/process-06-observability.md) — отдельный слой error_logs (автоматические)
- `feedback_triage` (WP-45) — LLM-классификация qa_history (не пользовательские reports)

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
