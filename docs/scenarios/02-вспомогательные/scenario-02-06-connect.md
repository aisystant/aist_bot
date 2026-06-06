# 02.06 `/connect` — подключение внешних AI клиентов

> Wizard для подключения внешних AI клиентов (claude.ai, Cursor, ChatGPT, Claude Code) к боту через Gateway MCP.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/connect` |
| Вид | Вспомогательная (B) — inline callback wizard |
| Файл | [`handlers/connect.py:52`](../../../handlers/connect.py) |
| FSM | Нет (inline callbacks, несколько экранов через `edit_text`) |
| Конфигурация | `GATEWAY_MCP_URL` из env |

---

## 1. Flow

```
/connect
  ↓
Menu (выбор клиента)
  ├─ claude.ai
  ├─ Cursor
  ├─ ChatGPT
  └─ Claude Code
  ↓
Instructions (screen per client)
  ├─ Copy Gateway URL
  ├─ Paste в settings клиента
  └─ Buttons: [Back] [Close]
```

## 2. Что показывает

Для каждого клиента — инструкция (i18n `connect.*`) с:
- Gateway MCP URL (`GATEWAY_MCP_URL`)
- Путь в настройках клиента
- Скриншот/описание как подключить MCP сервер

## 3. Источники

| Что | Откуда |
|-----|--------|
| URL | `GATEWAY_MCP_URL` (env) |
| Инструкции | i18n `connect.menu`, `connect.claude_ai`, `connect.cursor`, `connect.chatgpt`, `connect.claude_code` |

## 4. Связанное

- [P-10 Gateway MCP](../../processes/process-10-gateway-mcp.md) — архитектура Gateway
- WP-187 — OAuth flow для подключения клиентов (Ory)
- WP-210 — доработка E2E onboarding нового клиента

## 5. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/connect.py` | `cmd_connect` + callbacks |

---

## 6. Предусловие доступа

Команда `/connect` доступна любому зарегистрированному пользователю (прошедшему `/start`).
`onboarding_completed` не требуется — достаточно наличия записи в БД.

Ошибка "Сначала нажми /start" показывается только если `intern = None` (пользователь никогда не делал `/start`).

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
| 2026-06-06 | Gate упрощён: убрано требование `onboarding_completed` (WP-7 QAR5 fix, commit c3ff15b) |
