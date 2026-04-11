# 03.17 `/linear`

> Linear OAuth интеграция: статус, подключение, список задач, sync.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/linear` |
| Вид | Микро (C) |
| Файл | [`handlers/linear.py:29`](../../../handlers/linear.py) |
| Subcommands | `/linear disconnect`, `/linear tasks`, `/linear sync` |

---

## 1. Триггер и ветки

| Состояние | Что видит user |
|-----------|----------------|
| Не подключён | Кнопка `[🔗 Подключить Linear]` → OAuth |
| Подключён | Workspace name + кнопки `[📋 Задачи]` `[🔄 Sync]` `[Отключить]` |
| `/linear tasks` | Список актуальных задач из Linear API |
| `/linear sync` | Принудительная синхронизация |
| `/linear disconnect` | Очистка токена + подтверждение |

## 2. OAuth flow

Аналогично GitHub:
1. `clients/linear_oauth.get_authorization_url()` + UUID state
2. `oauth_pending_states` INSERT (`provider='linear'`)
3. Inline-кнопка со ссылкой на Linear OAuth consent
4. Callback сохраняет токены

## 3. Источники

| Что | Откуда |
|-----|--------|
| Токены | DB-таблица (имя уточнить — `linear_connections` или аналог) |
| Задачи | `clients/linear_api.py` — GraphQL API |

## 4. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/linear.py` | `cmd_linear` |
| `clients/linear_oauth.py` | OAuth |
| `clients/linear_api.py` | GraphQL calls |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
