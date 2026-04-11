# 03.16 `/github`

> GitHub OAuth интеграция: статус подключения, выбор репо для заметок, disconnect.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/github` |
| Вид | Микро (C) |
| Файл | [`handlers/github.py:41`](../../../handlers/github.py) |
| Subcommands | `/github disconnect`, `/github clear` |
| Таблица | `github_connections` |
| Tier gate | Нужен T4 для auto-publishing через Publisher |

---

## 1. Триггер и ветки

| Состояние | Что видит user |
|-----------|----------------|
| Не подключён | Кнопка `[🔗 Подключить GitHub]` → OAuth flow |
| Подключён | `@username` + `knowledge_repo` / `notes_path` + кнопки `[Отключить]` `[Сменить репо]` |
| `/github disconnect` | Очистка `access_token` + подтверждение |
| `/github clear` | Очистка настроек репо (без отключения) |

## 2. OAuth flow

1. `cmd_github` → `ory_oauth.get_authorization_url()` с `state = UUID4`
2. `oauth_pending_states` INSERT (`provider='github'`, `telegram_user_id`)
3. Redirect URL показан inline-кнопкой
4. Callback handler (`handlers/oauth_callback.py`) → `github_connections` UPDATE

**Источник:** [`clients/github_oauth.py`](../../../clients/github_oauth.py)

## 3. Что хранится

Поля `github_connections` (см. [tables.md §4.4](../../data/tables.md)):
- `access_token`, `token_type`, `scope`
- `github_username`
- `target_repo`, `notes_path` (для `fleeting-notes.md`)
- `strategy_repo`, `knowledge_repo`
- `default_branch` (определяется через GitHub API, §10.5 CLAUDE.md)

## 4. Правило default_branch

**§10.5 CLAUDE.md:** НЕ хардкодить `"main"`. При установке target_repo вызывать `GET /repos/{owner}/{repo}` → `default_branch`. Сохранять в БД. Retry заметок ограничен 3 попытками.

## 5. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/github.py` | `cmd_github`, subcommand routing |
| `clients/github_oauth.py` | OAuth |
| `clients/github_api.py` | Чтение/запись файлов, fleeting-notes insertion (§10.4) |
| `db/queries/github.py` | `github_connections` CRUD |

## 6. Связанное

- **Publisher (R21):** использует per-user OAuth tokens из `github_connections.knowledge_repo` (§10.5 CLAUDE.md). `GITHUB_BOT_PAT` — только для AutoFix в репо `aisystant/aist_bot`, не для публикаций user.
- **WP-53 Publisher:** `published_posts`, `scheduled_publications` — см. [tables.md §7](../../data/tables.md).

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
