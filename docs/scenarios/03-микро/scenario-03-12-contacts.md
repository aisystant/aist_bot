# 03.12 `/contacts`

> Контакты бота: email, соц. сети, ссылки поддержки.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/contacts` |
| Вид | Микро (C) |
| Файл | [`handlers/contacts.py:27`](../../../handlers/contacts.py) |

---

## 1. Триггер

- Пользователь вводит `/contacts` → `cmd_contacts` → `message.answer(t('contacts.text'), parse_mode='Markdown')`

## 2. Содержимое

**Текст:** чистый Markdown из i18n `t('contacts.text')` — email, Telegram-канал, поддержка, официальный сайт Aisystant, IWE.

**Кнопки:** отсутствуют.

**Важно:** это контакты **бота / команды Aisystant**, а не контакты самого user'а. Не путать с `/me` (дашборд user'а).

## 3. Источники

| Что | Откуда |
|-----|--------|
| Текст | i18n `contacts.text` |

## 4. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/contacts.py` | `cmd_contacts` |
| `i18n/translations/*.yaml` | `contacts.text` |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
