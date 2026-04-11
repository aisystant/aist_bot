# 02.09 `/navigator` — вход в роль Навигатор (R27)

> Явный вход в роль Навигатор (MIM.R.007) для сопровождения траектории развития. WP-156.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/navigator` |
| Вид | Вспомогательная (B) — FSM через consultation |
| Файл | [`handlers/commands.py:225`](../../../handlers/commands.py) |
| FSM | Маршрутизирует в `common.consultation` с `force_role='navigator'` |
| WP | WP-156 (explicit role entry) |
| Pack | [MIM.R.007 Навигатор](../../../../PACK-digital-platform/pack/mim/02-domain-entities/) |

---

## 1. Flow

```
/navigator
  ↓
Dispatcher.route_command('navigator')
  ↓
sm.go_to(user, 'common.consultation', context={force_role: 'navigator'})
  ↓
Consultation state
  ├─ System prompt: Навигатор (R27)
  ├─ Pre-search в knowledge-mcp
  └─ Диалог о траектории
```

## 2. Что делает Навигатор

Из `memory/roles.md` / `.claude/rules/role-prefixes.md`:

- Сопровождает ученика: адаптирует темп, объясняет «зачем», калибрует мотивацию
- **НЕ отвечает на предметные вопросы** (это Консультант / L3 general)
- Сценарии: С чего начать / не могу учиться / сколько помидорок / итоги недели / зачем учить
- **Принцип экзоскелета:** усиливать мышление, не заменять. Не решать за user'а. На ступенях 0-1 — конкретный первый шаг

## 3. Формат ответа

Максимум 7 пунктов, один экран. Рекомендации конкретные, ограниченные по времени (15-30 мин), проверяемые.

**Граница знаний:** НЕ додумывать факты. Нет данных — спросить (до 3 уточняющих вопросов).

## 4. Data

| Что | Откуда |
|-----|--------|
| Профиль user'а | `public.users` + `development.user_state` |
| ЦД (T3+) | `gateway_mcp.dt_read('3_derived')` — для персонализации |
| Knowledge | `gateway_mcp.knowledge_search` — Pack/guides |
| Role prompt | `.claude/rules/role-prefixes.md` / Pack MIM.R.007 |

## 5. Early role detection

`states/common/consultation.py` — функция `_detect_role(question)`. Если user префиксом «Навигатор, ...» вызывает роль в обычной консультации — переход в тот же L3-путь с `force_role='navigator'`. `/navigator` — ярлык без префикса.

## 6. Связанное

- [`/test`](scenario-02-10-test.md) — вход в R28 Диагност (парная роль)
- [P-08 Self-Knowledge](../../processes/process-08-self-knowledge.md) — L3 context pipeline
- `.claude/rules/role-prefixes.md` — описание роли и правил

## 7. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/commands.py` | `cmd_navigator` |
| `core/dispatcher.py` | `route_command('navigator')` |
| `states/common/consultation.py` | Consultation state + role detection |
| `engines/shared/question_handler.py` | `_build_user_profile` + role prompt injection |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
