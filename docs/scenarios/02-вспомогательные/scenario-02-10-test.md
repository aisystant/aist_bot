# 02.10 `/test` (alias `/assessment`) — диагностика ступени

> 12-вопросная диагностика для определения ступени мастерства и bottleneck (R28 Диагност, MIM.R.009).

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команды | `/test`, `/assessment` |
| Вид | Вспомогательная (B) — FSM wizard |
| Файл | [`handlers/commands.py:245`](../../../handlers/commands.py) |
| FSM state | `AssessmentFlowState` в `states/workshops/assessment/flow.py` |
| Result state | `workshop.assessment.result` |
| Таблица | `assessments` |
| Pack | [MIM.R.009 Диагност](../../../../PACK-digital-platform/pack/mim/02-domain-entities/) |

---

## 1. Модель ступеней (R28)

Случайный (0) → Практикующий (1) → Систематический (2) → Дисциплинированный (3) → Проактивный (4)

**Диагностическая карта (5 уровней):**
1. Состояния (сон, стресс, энергия)
2. Убеждения (мемы)
3. Привычки (слоты, рутины)
4. Среда (люди, инструменты)
5. Характеристики (целевые показатели)

## 2. Flow

```
/test
  ↓
Intro (объяснение диагностики)
  ↓
Question 1 ... Question 12 (inline choices)
  ├─ Каждый вопрос: 3-5 вариантов
  ├─ Ответ → UPDATE context → next question
  ↓
Self-check (user подтверждает: согласен с результатом?)
  ↓
Open question (свободный ответ — дополнительный контекст)
  ↓
workshop.assessment.result state
  ├─ Определение ступени 0-4
  ├─ Определение bottleneck (из 5 уровней карты)
  └─ Передача Навигатору (опционально)
```

## 3. Phase tracking

Все фазы (intro, questions, self_check, open) управляются внутри одного FSM state через `current_context` (JSONB в `development.user_state`), НЕ через отдельные SM-состояния. Это упрощает transitions.yaml и позволяет rewind к предыдущим вопросам.

## 4. Data

| Что | Откуда |
|-----|--------|
| Вопросы | `core/assessment.py` — `load_assessment(version)` |
| Scoring | `calculate_scores(answers)` → dominant_state |
| Сохранение | `assessments` INSERT (`answers`, `scores`, `dominant_state`, `self_check`, `open_response`) |
| Event | `assessment_completed` → `development.user_events` → `assessments_total` метрика |

## 5. Max questions

**Общее правило R28 (из `.claude/rules/role-prefixes.md`):** максимум 5 вопросов для диалога Диагноста. После — определять на основе имеющегося.

**`/test` отличается:** это структурированная анкета с 12 фиксированными вопросами, не свободный диалог. Диагност-диалог — это консультация с префиксом «Диагност, ...».

## 6. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/commands.py` | `cmd_test` / `cmd_assessment` |
| `core/assessment.py` | `load_assessment`, `calculate_scores` |
| `states/workshops/assessment/flow.py` | FSM state с phase-dispatch |
| `db/queries/assessments.py` | CRUD для `assessments` |

## 7. Связанное

- [`/navigator`](scenario-02-09-navigator.md) — парная роль R27 (диалог после диагностики)
- [metrics.md § 3.1](../../data/metrics.md) — `assessments_total`

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
