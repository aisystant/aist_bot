# Процессы

> Внутренние процессы работы бота. Описывают **как** бот работает изнутри.

---

## Отличие от Сценариев

| Категория | Описывает | Аудитория | Пример |
|-----------|-----------|-----------|--------|
| **Сценарий** | Взаимодействие пользователя с ботом | Продукт, QA | «Пользователь нажимает /progress → видит статистику» |
| **Процесс** | Внутреннюю логику бота | Разработка | «При ответе вызывается record_active_day() → обновляется streak» |

---

## Список процессов

| № | Процесс | Описание | Файл |
|---|---------|----------|------|
| P-01 | [Отслеживание активности](process-01-activity-tracking.md) | Запись активных дней, расчёт streak | `db/queries/activity.py` |
| P-02 | [Генерация контента](process-02-content-generation.md) | Генерация уроков, заданий, дайджестов через Claude API | `states/workshops/marathon/*.py`, `clients/claude.py` |
| P-03 | [Определение интента](process-03-intent-detection.md) | Классификация сообщений пользователя | `bot.py` (устаревает при USE_STATE_MACHINE=true) |
| P-04 | [Сбор статистики](process-04-stats-collection.md) | Агрегация данных для /progress | `db/queries/`, `states/utilities/progress.py` |
| P-05 | [Локализация](process-05-i18n.md) | Многоязычный интерфейс (ru/en/es/fr) | `i18n/` |
| P-16 | [Publisher: content scan + backfill](process-16-publisher-content-scan.md) | Скан индекса знаний, auto-schedule в клуб, еженедельный backfill просроченных `ready` | `core/scheduler.py` |
| P-17 | [GitHub App Install — Privacy Enforcement](process-17-github-app-install-privacy-enforcement.md) | Проверка и принудительная приватность репозитория после установки GitHub App в `/connect_guide` | `clients/github_app.py`, `oauth_server.py` |
| P-18 | [Контракт выпуска — pilot → production](process-18-release-contract.md) | Смысл дрейфа, разрешённые различия сред, классификация дельт, audit-trail `incident-ok` (WP-562 Ф1) | `.githooks/pre-push`, `release-governance/incident-ok/` |
| P-20 | [Свободный чат через агента платформы](process-20-interlocutor-flag.md) | Флаг: вопрос владельца уходит в агента `iwe-interlocutor` с пропуском человека, запасной путь прежний (РП-262 Ф4) | `engines/shared/interlocutor_client.py`, `states/common/consultation.py` |
| P-21 | [Единая запись быстрой заметки](process-21-fleeting-note-save-path.md) | Заметка с точкой, пересланная, голосовая и от Siri пишется одной функцией: те же проверки, пометка источника, один след для платформы, отмена голосовой, строка замера (WP-384) | `core/fleeting_notes.py` |

---

## Связь с данными

Каждый процесс описывает:
- **Входные данные** — откуда берёт информацию
- **Выходные данные** — что записывает в БД
- **Ссылки на таблицы** — из `docs/data/tables.md`

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-01-23 | Создание раздела |
| 2026-02-03 | Обновлены ссылки на файлы State Machine |
| 2026-09-09 | +P-18 (контракт выпуска pilot → production, WP-562 Ф1) |
| 2026-11-28 | +P-21 (единая запись быстрой заметки, WP-384) |
