# 03.13 `/twin`

> Статус подключения Цифрового Двойника (ЦД) и управление подключением.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/twin` |
| Вид | Микро (C) |
| Файл | [`handlers/twin.py:183`](../../../handlers/twin.py) |
| Subcommand | `/twin disconnect` |
| Архитектурное правило | Бот **read-only** для `3_derived` — calculator в Profiler (WP-218 Ф2) |

---

## 1. Триггер и ветки

| Состояние | Что видит пользователь |
|-----------|------------------------|
| **ЦД не подключён** | Текст «Подключи ЦД» + кнопка OAuth через Ory |
| **ЦД подключён** | Dashboard: основные индикаторы из `3_derived` + кнопка «Подробнее» |
| **`/twin disconnect`** | Confirmation → при подтверждении чистит `dt_tokens` + `ory_tokens` |

## 2. Источники данных

| Что | Откуда |
|-----|--------|
| Статус подключения | `gateway_mcp.is_connected(telegram_user_id)` |
| Данные для dashboard | `gateway_mcp.dt_read('3_derived', telegram_user_id)` — pure reader |
| Имя/профиль | `gateway_mcp.get_user_profile(telegram_user_id)` |

**Правило (§12c CLAUDE.md, WP-218 Ф2):** бот НЕ вычисляет `3_derived`. Только читает. Все числа в UI — напрямую из `digital_twins.data['3_derived']`, записанного `DS-ai-systems/profiler/scripts/recalculate_derived.py`.

**IND-комментарии:** каждое число в UI `/twin` должно иметь `IND.N.N.N` ссылку на метамодель (трассируемость). См. WP-218 Ф3 + WP-221 (генератор констант).

## 3. Anti-hallucination (WP-139)

**Правило §10 пункт 6:** нулевые метрики ≠ отсутствие данных. Если `3_derived` пусто или данных нет:
- ❌ НЕ выдумывать числа (Claude склонен выводить из косвенных источников)
- ✅ Явно показать «данные не подключены» или «профиль пуст, НЕ угадывай»

## 4. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/twin.py` | `cmd_twin`, `show_twin_dashboard` |
| `clients/gateway_mcp.py` | `dt_read`, `is_connected`, `get_user_profile` |
| `clients/ory_oauth.py` | OAuth flow |

## 5. Связанные процессы

- [P-07 DT Engagement Sync](../../processes/process-07-dt-engagement-sync.md) — как данные попадают в `digital_twins`
- [P-10 Gateway MCP](../../processes/process-10-gateway-mcp.md) — `dt_read` wrapper
- [P-08 Self-knowledge](../../processes/process-08-self-knowledge.md) — `/twin` использует L3 консультацию для insights

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-30 | Fix: /twin fallback на development.engagement когда ЦД пуст (WP-218 Ф2) |
| 2026-04-30 | Fix: /twin race condition — use safe get_user_profile() pattern |
| 2026-04-11 | Создание документа (DOC1.C batch) |
