# 01.03 Сценарий Тренировки (`/train`)

> Режим Тренировки: разминка на принципах Земенфлюка (ZP.1..ZP.6). Многошаговый FSM-флоу с глубиной (depth) и auto-generated заданиями.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/train` |
| Вид | Основной (A) — учебный режим |
| Handler | [`handlers/commands.py:105`](../../../handlers/commands.py) |
| Engine | `engines/training/engine.py` |
| FSM states | `TrainingDashboardState`, `TrainingAssignmentState`, `TrainingChildAssignmentState` (в `states/training/`) |
| Таблицы | `training_settings`, `training_progress`, `training_attempts`, `training_children` (family mode) |
| Константы | `ZP_PRINCIPLES`, `TRAINING_MAX_DEPTH`, `KID_MAX_DEPTH` (для child режима) |

---

## 1. Концепция

Тренировка — это «разминка» на 6 принципах из Pack'а (ZP.1..ZP.6, «Земенфлюк»). Для каждого принципа есть **глубина** (depth 0-3) — уровни освоения. User получает задание → даёт ответ → получает feedback → проходит на следующую глубину.

**Family mode (WP-55 Phase 2):** родитель может тренироваться «за ребёнка» (отдельный `child_id`, собственные `training_progress` и `cognitive_level` = `concrete_operational`).

## 2. FSM-флоу

```
/train
  ↓
Dashboard (TrainingDashboardState)
  ├─ Список принципов с текущим depth
  ├─ Выбор принципа / child mode
  └─ [Начать] → TrainingAssignmentState
     ↓
TrainingAssignmentState
  ├─ Генерация задания (через Claude)
  ├─ Показ задания
  └─ Ожидание ответа
     ↓
Проверка ответа
  ├─ passed=True → depth += 1 → обратно в Dashboard
  └─ passed=False → feedback → повтор
```

## 3. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/commands.py` | `cmd_train` — тонкий handler через dispatcher |
| `core/dispatcher.py` | `_LEGACY_COMMAND_MAP`: `/train → training.dashboard` |
| `engines/training/engine.py` | `load_cells()`, `generate_assignment()`, `report_result()`, dashboard data |
| `states/training/dashboard.py` | FSM dashboard state |
| `states/training/assignment.py` | FSM assignment state |
| `states/training/child_assignment.py` | Child mode variant |
| `db/queries/training.py` | CRUD для training_* таблиц |

## 4. Data flow

1. **Setup check:** если `training_settings` нет → возврат `setup_needed` → onboarding в настройки тренировки
2. **Dashboard:** `get_training_dashboard_data(chat_id)` читает `training_settings` + `training_progress` → список `{principle, depth, attempts}`
3. **Assignment:** `generate_child_assignment()` или `generate_assignment()` (base) вызывает Claude через `clients/claude.py`
4. **Answer:** user пишет ответ → `report_result(chat_id, principle_id, depth, answer)` → LLM-анализ → `passed: bool`
5. **Store:** INSERT в `training_attempts` (история) + UPDATE `training_progress` (current_depth, attempts_at_depth)

## 5. Правило wire-up (§13 CLAUDE.md)

> **Data-файл + load-функция ≠ работающая фича.** При добавлении нового набора ячеек обновить ВСЕ 6 мест:
> 1. `engines/training/engine.py` — импорт `load_*_cells`, `*_PRINCIPLES`, `*_MAX_DEPTH`
> 2. `generate_child_assignment()` — `load_*_cells()` + MAX_DEPTH
> 3. `report_child_result()` — `load_*_cells()`
> 4. `get_child_dashboard_data()` — PRINCIPLES + MAX_DEPTH + name func
> 5. `get_next_child_principle()` — PRINCIPLES + MAX_DEPTH
> 6. `states/training/child_assignment.py` — импорт `KID_MAX_DEPTH`, `get_kid_principle_name`
>
> Без всех 6 мест фича молча не работает (бот возвращает None, принципы не показываются).

## 6. Event logging

При каждом attempt → `log_event('training_attempt', payload={passed, depth, principle_id})` → `development.user_events`. Агрегируется в VIEW `development.engagement` как `training_attempts_total` и `training_passed_total` (см. [metrics.md §3.1](../../data/metrics.md)).

## 7. Связанные сценарии и процессы

- [`/train_info`](../03-микро/scenario-03-09-train-info.md) — info-экран про тренировку (микро)
- [P-02 Content Generation](../../processes/process-02-content-generation.md) — генерация контента (Claude)
- [P-06 Observability](../../processes/process-06-observability.md) — event stream и метрики

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C) |
