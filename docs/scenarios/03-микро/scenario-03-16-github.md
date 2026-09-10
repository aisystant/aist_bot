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

## 7. GitHub App путь (WP-406 Ф22, флаг `GITHUB_APP_NOTES_ENABLED`)

> Триггер: реальный отказ участника марафона подключать бота — OAuth scope
> `repo` даёт запись во ВСЕ репозитории аккаунта (WP-458 ВЫ-13).

**Флаг выключен по умолчанию.** При `GITHUB_APP_NOTES_ENABLED=true` и заданном
`GITHUB_APP_SLUG` новые подключения (`cmd_github`, нет ни OAuth, ни App-связки)
ведут не на OAuth authorize URL, а на установку GitHub App с
`repository_selection=selected` — пользователь выбирает конкретные 1-3
репозитория на стороне GitHub, а не выдаёт доступ ко всему аккаунту.

**Общая установка с Персональным руководством (WP-301).** У пользователя ОДНА
`app_installation_id` в `github_connections` — если он подключил и
Персональное руководство, и заметки, это одна и та же установка App. Отсюда:
- **Приоритет источника токена** ([`clients/github_auth.py`](../../../clients/github_auth.py)`::resolve_auth_context`):
  активная установка > OAuth. OAuth WRITE разрешена ещё `GITHUB_APP_OAUTH_GRACE_UNTIL`
  дней (включительно по конец UTC-дня), READ — всегда.
- **Список репозиториев для выбора** (`callback_github_select_repo` и т.п.):
  при активной установке — только `get_installation_repos()`, не полный
  список аккаунта.
- **Disconnect заметок НЕ трогает установку.** `disconnect_github_notes`
  (`db/queries/github.py`) очищает только OAuth/notes-колонки
  (`target_repo`, `knowledge_repo`, `access_token_encrypted` и т.д.);
  `app_installation_id`/`app_repo_full_name` не изменяются — иначе отключение
  заметок сломало бы Персональное руководство той же установки. Все три
  вызывающих места (`clients/github_oauth.py::disconnect`, `/mydata` хаб)
  идут через эту функцию, не напрямую через `delete_github_connection`.
- **Install-callback** (`oauth_server.py::github_app_callback_handler`)
  сохраняет текущий `app_repo_full_name`, если он ещё в списке репозиториев
  установки — не перезаписывает его безусловно первым репо при повторном
  срабатывании callback (например, GitHub Configure).

**Не мигрировано в этой фазе:** `handlers/strategist.py` (репо стратега) и
`clients/github_content.py`/`github_strategy.py` (Публикатор/стратегия) —
остаются на чистом OAuth без изменений, не регрессия.

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
| 2026-09-10 | +§7: GitHub App путь (WP-406 Ф22), disconnect safety, приоритет токена |
