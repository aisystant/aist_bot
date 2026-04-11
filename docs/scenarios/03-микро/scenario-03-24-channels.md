# 03.24 `/channels`

> Список отслеживаемых каналов (SC.118 Channel Mentions Assistant). Управление мониторами: вкл/выкл, настройка детекции.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/channels` |
| Вид | Микро (C) |
| Файл | [`handlers/channels.py:68`](../../../handlers/channels.py) |
| Таблицы | `channel_monitors`, `channel_mentions_log` |
| Tier gate | T2+ (подписка или trial) + `onboarding_completed=TRUE` |
| Связанное | [`P-06 § SC.118`](../../processes/process-06-observability.md) |

---

## 1. Триггер и ветки

| Состояние | Что видит user |
|-----------|----------------|
| T2+, есть мониторы | Список каналов с статусами (активный / пауза) + кнопки управления |
| T2+, нет мониторов | Инструкция «Добавь бота в канал как админа» |
| T0-T1 | Paywall «Требуется подписка или триал» |
| Не онбординг | Перенаправление на `/start` |

## 2. Auto-discovery каналов

**§10.30 CLAUDE.md:** при первом сообщении из канала без мониторов → `getChatMember` для всех пользователей бота → создание мониторов для найденных админов. Кэш `_discovered_channels` предотвращает повторный перебор.

## 3. Настройки монитора

Для каждого `channel_monitors` можно включить/выключить:
- `track_username` — детект по `@username`
- `track_reply` — детект по reply к сообщениям пользователя
- `track_name` — детект по имени (из профиля)

## 4. Mention flow (§10.30)

1. `channel_post` / `message` из канала → `detect_mentions()`
2. Если mention найден и user — админ канала → **draft через Opus + knowledge-mcp** → отправка в личку
3. Если user — не админ, только участник → **простое уведомление**
4. Cooldown 30 сек между уведомлениями
5. `log_before_send` в `channel_mentions_log` + UNIQUE(channel_id, message_id, mentioned_chat_id)

## 5. Контекст черновика (для админа)

Три источника (см. §10.30):
1. **Владелец** — профиль из ЦД (`1_declarative`) через Gateway MCP
2. **Канал** — `config/channel_contexts.yaml` (title pattern → описание/аудитория/тон)
3. **Knowledge** — расширенный поиск через `knowledge-mcp` (до 5 результатов × 500 символов)

## 6. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/channels.py` | `cmd_channels` |
| `core/mention_detector.py` | `detect_mentions()` |
| `db/queries/channels.py` | CRUD monitors |
| `config/channel_contexts.yaml` | Config каналов (tone, audience) |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
