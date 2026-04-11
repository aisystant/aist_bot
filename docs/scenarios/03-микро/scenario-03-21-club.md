# 03.21 `/club`

> Discourse интеграция: статус подключения к клубу, публикация постов, subcommands.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/club` |
| Вид | Микро (C) |
| Файл | [`handlers/discourse.py:156`](../../../handlers/discourse.py) |
| Subcommands | `/club connect`, `/club publish`, `/club disconnect` |
| Таблица | `discourse_accounts` |
| Требования | `DISCOURSE_API_URL` в env |

---

## 1. Триггер и ветки

| Состояние | Что видит user |
|-----------|----------------|
| Без подключения | Инструкция + `/club connect` |
| `/club connect` | Стартует OAuth flow к Discourse |
| Подключён | Статус + `[📝 Опубликовать]` + `[Отключить]` |
| `/club publish` | Запускает publisher flow → выбор файла → scheduled_publications |
| `/club disconnect` | Очистка `discourse_accounts` |

## 2. Publishing flow (§10.6 Publisher правила)

При публикации:
1. Источник — файл из GitHub-репо пользователя (`knowledge_repo`, `strategy_repo`)
2. Scheduler в cron `_discourse_scheduled_publish` (каждые :07, :37) запускает очередь из `scheduled_publications WHERE status='pending' AND schedule_time <= NOW()`
3. При cancel — ОБЯЗАТЕЛЬНО revert frontmatter `status: ready → draft` через GitHub API (§10.6 CLAUDE.md)
4. Итоги недели (тег `итоги-недели`) публикуются сразу

**Slot generation:** `occupied_dates` set — одна дата = один пост. `PUBLISHER_INTERVAL` (env, default=2) — минимум N дней между публикациями.

## 3. Источники

| Что | Откуда |
|-----|--------|
| Discourse аккаунт | `discourse_accounts` |
| Категория блога | `discourse_accounts.blog_category_id` |
| Файлы | `clients/github_content` через `knowledge_repo` |
| Публикации | `published_posts`, `scheduled_publications` |

## 4. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/discourse.py` | `cmd_club` + subcommand dispatch |
| `clients/discourse.py` | Discourse API |
| `db/queries/discourse.py` | DB операции |
| `core/scheduler.py` | `_discourse_scheduled_publish`, `_smart_publisher_scan` |

## 5. Связанные таблицы

См. [tables.md §4.6, §7.2, §7.3](../../data/tables.md):
- `discourse_accounts` — OAuth связь
- `published_posts` — опубликованные
- `scheduled_publications` — очередь

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
