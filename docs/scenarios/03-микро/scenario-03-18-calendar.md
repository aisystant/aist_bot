# 03.18 `/calendar`

> Google Calendar OAuth интеграция: статус подключения, показ событий дня, disconnect.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/calendar` |
| Вид | Микро (C) |
| Файл | [`handlers/google_calendar.py:34`](../../../handlers/google_calendar.py) |
| Subcommand | `/calendar disconnect` |
| Таблица | `google_calendar_connections` |
| WP | WP-128 |

---

## 1. Триггер и ветки

| Состояние | Что видит user |
|-----------|----------------|
| Не подключён | Кнопка `[📅 Подключить Google Calendar]` → OAuth |
| Подключён | Email + события на сегодня + кнопка `[Отключить]` |
| `/calendar disconnect` | Очистка токенов |

## 2. OAuth flow

1. `cmd_calendar` → `clients/google_calendar_oauth.get_authorization_url()` с scope `calendar.readonly`
2. `oauth_pending_states` INSERT (`provider='google_calendar'`)
3. Redirect на Google OAuth consent
4. Callback → `google_calendar_connections` INSERT/UPDATE

## 3. Что хранится

Поля `google_calendar_connections` (см. [tables.md §4.5](../../data/tables.md)):
- `access_token`, `refresh_token`, `expires_at`
- `email`

## 4. Источники

| Что | Откуда |
|-----|--------|
| Токены | `google_calendar_connections` |
| События | `clients/google_calendar.list_events_today(access_token)` |

## 5. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/google_calendar.py` | `cmd_calendar` |
| `clients/google_calendar_oauth.py` | OAuth |
| `clients/google_calendar.py` | API calls |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
