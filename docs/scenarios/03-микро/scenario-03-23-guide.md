# 03.23 `/guide`

> Entry point в руководства Aisystant. T2 и ниже требуют подключить ЦД перед входом.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/guide` |
| Вид | Микро (C) |
| Файл | [`handlers/guide.py:41`](../../../handlers/guide.py) |
| Tier gate | T2 и ниже — нужно подключить ЦД через Ory OAuth |

---

## 1. Триггер и ветки

| Tier | Что видит user |
|------|----------------|
| **T0-T2** | Paywall «Для доступа к руководствам подключи ЦД» + кнопка OAuth через Ory |
| **T3+** | Ссылка на руководства Aisystant + доступ через Gateway MCP |

## 2. Tier-логика

Tier T3 требует подключённый ЦД (OAuth через Ory). `detect_ui_tier(chat_id)` проверяет:
- Aisystant подписку (T2)
- Наличие `ory_id` в `public.users` + валидный токен в `ory_tokens` → T3
- GitHub OAuth → T4

**Paywall правило (§12 CLAUDE.md):** текст НЕ ДОЛЖЕН обещать функциональность, отсутствующую у целевой команды. `/guide` показывает только реальные Aisystant guides, не выдуманные.

## 3. Источники

| Что | Откуда |
|-----|--------|
| Tier | `detect_ui_tier()` |
| Guides API | `gateway_mcp.knowledge_search` / `gateway_mcp.get_instructions` |
| OAuth URL | `clients/ory_oauth.get_authorization_url()` |

## 4. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/guide.py` | `cmd_guide` |
| `clients/gateway_mcp.py` | Knowledge search |
| `clients/ory_oauth.py` | OAuth |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
