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
| `/club connect` | Просьба прислать username+URL; ownership-check ниже |
| Подключён | Статус + `[📝 Опубликовать]` + `[Отвязать]` + подсказка «не мой аккаунт» |
| `/club publish` | Запускает publisher flow → выбор файла → scheduled_publications |
| `/club disconnect` | Очистка `discourse_accounts` |

### 1a. Connect ownership-check (WP-7 DC1+DC2, 27 апр 2026)

При подключении бот проверяет, что Telegram-юзер реально владеет указанной категорией. Discourse-семантика: блог-категория принадлежит личной группе юзера (`user-N`), которая получает `permission_type=1` (full edit) в `category.group_permissions`. Проверка: пересечение `user.groups` с группами категории, владеющими ею.

| Сценарий | Действие |
|----------|----------|
| URL `c/blogs/blogs-user-N/<id>` без явного username | Reject: «Ссылка содержит slug категории, не username — пришли username отдельно» |
| Username не существует в Discourse | Reject: «Пользователь не найден в клубе» |
| Username существует, но НЕ владеет category_id | Reject: «Категория принадлежит другому юзеру» |
| Категория без явного владельца (только `everyone`) | Reject: «Общая категория клуба, в неё через бота нельзя» |
| Все проверки PASS | INSERT в `discourse_accounts` + клавиатура с кнопкой «✗ Это не мой аккаунт» |

**Helpers** (`handlers/discourse.py`):
- `_category_owner_groups(cat)` — извлекает группы с `permission_type=1`, исключая `everyone`
- `_user_is_category_owner(user, cat)` — проверяет пересечение групп

**Эвристика-предшественник удалена.** До 27 апр 2026 функция `_resolve_username_from_category` угадывала владельца по названию категории через slugify+search — это позволяло привязать чужой блог к Telegram-юзеру (incident WP-7 DC: Андрей↔Tseren).

**Тесты:** `tests/smoke/test_discourse_ownership.py` (9 кейсов).

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
| 2026-04-27 | WP-7 DC1+DC2: ownership-check через `category.group_permissions` (вместо эвристики `_resolve_username_from_category` + slug-парс), inline-кнопка «✗ Это не мой аккаунт». Тесты `test_discourse_ownership.py`. |
