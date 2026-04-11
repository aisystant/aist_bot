# 03.15 `/mydata`

> Hub управления персональными данными: просмотр, экспорт, удаление. 5 секций с inline-навигацией.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/mydata` |
| Вид | Микро (C) — однократный entry point, дальше inline sub-navigation |
| Файл | [`handlers/commands.py:166`](../../../handlers/commands.py) |
| Dispatch | Через `Dispatcher.route_command('mydata', intern)` |
| Требования | Онбординг завершён |
| SM поддержка | Опционально: если доступен `utility.mydata` state, маршрутизируется туда |

---

## 1. Триггер и маршрут

1. Пользователь вводит `/mydata`
2. Handler проверяет `onboarding_completed` — если нет, предлагает пройти `/start`
3. `Dispatcher.route_command('mydata')` → либо SM state `utility.mydata`, либо inline-hub

## 2. 5 секций (inline кнопки)

| Секция | Что показывает | Источник |
|--------|----------------|----------|
| **Профиль** | Имя, занятие, цели, язык | `public.users` |
| **Активность** | Streaks, даты, марафон-статус | `development.user_state`, `activity_log` |
| **Ответы** | Сводка ответов (theory, work_product, fixation) | `answers` |
| **Интеграции** | GitHub / Ory / DT / Google Cal / Linear / Wakatime / Discourse — статус | соответствующие `*_connections` таблицы |
| **Удаление данных** | Кнопка «Удалить всё» → confirm через text input | `delete_all_user_data()` |

## 3. Правила навигации (§10.32)

- **Не удалять исходное сообщение** при drill-down (`edit_text` через inline kbd)
- **Callback не должен отправлять текстовую подсказку** «используй /mydata» — запускай через `Dispatcher.route_command()`
- **Back** через `delete` + `enter` (§10.9) в sub-меню

## 4. Удаление (GDPR)

**Confirm flow:** кнопка «Удалить всё» → bot отправляет сообщение с просьбой ввести «УДАЛИТЬ» → handler на следующее текстовое сообщение сверяет → `delete_all_user_data(chat_id)`.

**Хранение context:** `awaiting_delete` → в `development.user_state.current_context` (не в `fsm_states.data` — §10.35).

## 5. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/commands.py` | `cmd_mydata` |
| `core/dispatcher.py` | `route_command('mydata')` |
| `states/utilities/mydata.py` | (опциональный) SM state с inline hub |
| `db/queries/users.py` | `delete_all_user_data()` |

## 6. Связанное с Pack

WP-214 — концепция учёта персональных данных в IWE. 13 принципов включая:
- Явный dashboard видимых данных
- Право на экспорт (TODO)
- Право на удаление (реализовано)
- Roles-based доступ (TODO)

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
