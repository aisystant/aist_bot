# Сценарий C-33: Day Open дайджест (/day)

**Класс:** Микро (Вид C)
**РП:** WP-428 Ф3
**Handler:** `handlers/day.py` — `on_day`
**Команда:** `/day`

## Триггеры

| Ввод | Роутер |
|------|--------|
| `/day` | `day_router` (Command filter) |

## Поведение по тирам

| Тир | Поведение |
|-----|-----------|
| T0/T1 | CTA: "Оформи подписку: /subscribe" |
| T2 | CTA: "Подключи IWE через /connect" |
| T3+ | Полный дайджест: ритм + активные РП + фокус |

### T3+ full digest

1. Pre-fetch из rewards DB (Railway): `get_today_total(ory_id)`, `get_earned_total(ory_id)` — sequential, вне keep_typing
2. `keep_typing(message)` стартует
3. `gateway_mcp.hermes_chat(message=HERMES_PROMPT, telegram_user_id=chat_id)` — подгружает user context через Aisystant MCP
4. Собирается `full_msg = rhythm_header + hermes_response`
5. Отправляется с `parse_mode="Markdown"`, fallback — без parse_mode

### Частичный дайджест (hermes_chat failed)

Если hermes_chat упал — показывается только ритм-строка + "Сейчас недоступно. Попробуй позже."

## SM guard

Проверяется до tier-detection:
- `_sm_is_expecting_reply(chat_id)` — async, из `handlers/external_session`
- `FeedDigestState.is_waiting_fixation(chat_id)` — sync, из `states.feed.digest`

При активном SM → `raise SkipHandler` (передаём управление следующему роутеру).

## Формат ответа (T3+)

```
📅 Открытие дня

Ритм: 42 балла сегодня  •  Всего: 1 840

<hermes_chat response: 3-5 строк>
1-2 активных РП (название + что дальше)
Одна фокус-задача на сегодня
```

## SLA

- Pre-fetch rewards DB: <100ms
- hermes_chat: ≤60s (keep_typing активен)
- Общий `/day` → ответ: **≤60s**

## Почему rewards pre-fetch нужен (не через hermes_chat)

`dt_sync.sync_engagement_to_dt()` синхронизирует engagement/coding/LMS в Digital Twin,
но НЕ синхронизирует `rewards.applied_events` (Railway rewards pool). Поэтому
`hermes_chat.get_user_context` не видит today's points — нужен прямой запрос.

## Known limitations (Ф3 MVP)

- **Weekly slots** (инвестированные часы/неделю) — нет готового запроса в bot DB; defer to Ф3б
- **Double /day** — два параллельных hermes_chat вызова при быстром двойном tap; известное ограничение, не блокер
- **Active WPs accuracy** — зависит от hermes_chat / get_user_context; T3 (без GitHub T4) может иметь неполный список РП

## Связанные артефакты

- `handlers/day.py` — реализация
- `handlers/iwe.py` — паттерны Ф2 (tier guard, keep_typing, PII guard, try/except parse_mode)
- `inbox/WP-428/WP-428.md` — контекст РП
- `DP.SC.154` — peer-session протокол
