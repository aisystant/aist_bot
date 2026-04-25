# 03.11 `/status`

> Статус платформы Aisystant — ссылка на public dashboard и канал инцидентов.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/status` |
| Вид | Микро (C) — одно сообщение + 2 inline-кнопки |
| Файл | [`handlers/status.py`](../../../handlers/status.py) |
| Доступность | Все пользователи (любой tier) |
| Источник | WP-244 Ф7 (DP.SC.124 User-Facing Platform Health) |

---

## 1. Триггер и эффект

- Пользователь вводит `/status` → `cmd_status`
- Handler рендерит статичное сообщение с двумя ссылками (Markdown HTML)
- Без вызовов API — мгновенный ответ (нет live-fetch состояний)

## 2. Содержимое

**Текст** (HTML, hardcoded на русском — статус универсален для всех языков):
- 📊 Ссылка на public dashboard: https://aisystant.betteruptime.com
- 📢 Ссылка на канал инцидентов: @aisystant_status

**Inline-кнопки:**
- «📊 Открыть dashboard» → URL https://aisystant.betteruptime.com
- «📢 Подписаться на канал» → URL https://t.me/aisystant_status

## 3. Связи

| Что | Где |
|-----|-----|
| Public status page | https://aisystant.betteruptime.com (Better Stack) |
| TG-канал инцидентов | https://t.me/aisystant_status |
| CF Worker (источник постов в канал) | https://observability-webhook.aisystant.workers.dev |
| Service Clause | DP.SC.124 User-Facing Platform Health |
| Родительский РП | WP-244 Platform Observability |

## 4. Регистрация

- `handlers/__init__.py` — импорт `status_router` + `dp.include_router(status_router)`
- `bot.py` — добавлен `BotCommand("status", "Статус платформы")` для всех языков (ru/en/es/fr/zh)

## 5. Phase 2 (опц., при необходимости)

Live-fetch из Better Stack API (через тот же `BETTERSTACK_API_TOKEN` что у CF Worker'а):
- Composite uptime по 3 monitors (multiplicative)
- Активные инциденты (count + ссылка на каждый)
- Последний resolved инцидент (когда / длительность)

Реализовать через `clients/betterstack.py` если live-fetch потребуется. На MVP — статичный текст достаточен (быстро, без секретов в боте).
