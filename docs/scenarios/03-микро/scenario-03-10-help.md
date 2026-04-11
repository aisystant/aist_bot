# 03.10 `/help`

> Справка по функциям бота: тиры, AI-консультации, заметки, подписка, фидбэк, смена языка.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/help` |
| Вид | Микро (C) |
| Файл | [`handlers/settings.py:156`](../../../handlers/settings.py) |
| Event-logging | `event_type='help_viewed'` → `development.user_events` (WP-151 Ф3) |

---

## 1. Триггер и эффект

- Пользователь вводит `/help` → `cmd_help`
- Handler логирует событие `help_viewed` в `development.user_events` (метрика `help_views_total`, см. [metrics.md § 3.2](../../data/metrics.md))
- Рендерит i18n-текст

## 2. Содержимое

**Текст из i18n** (`t('help.*')`), несколько секций:
- Intro (что умеет бот)
- Тиры (T0-T4, что открывается)
- AI-консультация: как задать вопрос (префикс `?` или `/question`)
- Заметки: как сохранять через GitHub
- Подписка: как оформить
- Фидбэк: `/feedback`
- Смена языка: `/language`

**Кнопки:** отсутствуют (чистый текст).

## 3. Источники

| Что | Откуда |
|-----|--------|
| Текст | i18n `help.intro`, `help.tiers`, `help.ai`, `help.notes`, `help.subscription`, `help.feedback`, `help.language` |
| Registry команд | `core.registry.get_all_commands()` (для подстановки актуального списка) |

## 4. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/settings.py` | `cmd_help` |
| `core/registry.py` | ServiceRegistry |
| `i18n/translations/*.yaml` | `help.*` |
| `db/queries/events.py` | `log_event('help_viewed')` |

## 5. Правила

- **Anti-hallucination (§10.4):** текст НЕ ДОЛЖЕН обещать функциональность, отсутствующую у целевой команды. Обновляй `help.*` при добавлении/удалении команд.
- **Ключ `help.about_marathon` проблемный** (§10.1): если ключ отсутствует в schema, `t()` вернёт строку `"help.about_marathon"` с `_` → Telegram интерпретирует как курсив → crash. Всегда проверять наличие ключа.

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
