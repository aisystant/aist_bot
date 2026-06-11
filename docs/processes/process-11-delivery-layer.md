# Процесс 11 — Слой доставки (Доставщик)

> **Pack:** see DP.SC.177 (обещание), DP.ROLE.075 (роль). **РП:** WP-418 Ф3.
> **Код:** `core/notification_service.py`, очередь в `db/models.py` (`notification_queue`), дренаж в `core/scheduler.py` (`_drain_delivery_queue`).
> **Флаг:** `DELIVERY_LAYER_ENABLED` (по умолчанию `false`).

## Зачем

Доставка размазана: ~107 точек прямой отправки в Telegram, ~20 проактивных отправителей, единого слоя нет. Нельзя обещать «не больше N сообщений в день», нельзя приоритизировать (урок важнее нуджа), нельзя единообразно уважать предпочтения. Доставщик — единая воронка всех исходящих, которая считает / ограничивает / приоритизирует / дедуплицирует / уважает предпочтения по ВСЕМ сообщениям.

## Три слоя (не путать)

| Слой | Кто | Что делает |
|------|-----|-----------|
| Решение | Nudge Engine (DP.SC.116, РП117) | ЧТО и КОГДА слать по смыслу, владеет opt-out категориями |
| **Политика (этот процесс)** | **Доставщик (DP.ROLE.075)** | **потолок класса · приоритет · дедуп · hard-gate предпочтений** |
| Транспорт | Notification Dispatcher (DP.ROLE.044, процесс 09) | физическая доставка exactly-once в Telegram |

## Класс-модель (5 классов)

| Класс | Дневной лимит | Приоритет | Дедуп | Канал |
|-------|---------------|-----------|-------|-------|
| `critical` (безопасность/деньги) | без лимита, обходит всё | 1 | 24 ч | user |
| `must-deliver` (урок, чек-ин подписки) | вне потолка | 2 | 12 ч | user |
| `transactional` (`/remind`, OAuth) | без лимита, дедуп | 3 | 6 ч | user |
| `capped` (реанимация, nudge) | **≤2/день суммарно** | 4 | 48 ч | user |
| `ops-alert` (autofix/health) | без лимита | 9 | 1 ч | dev |

Ключевая гарантия: потолок `capped` считается по ВСЕМ источникам суммарно, а не у каждого отправителя отдельно (урок РП406).

## Поток

```
in-bot отправитель → enqueue(chat_id, klass, content_spec, priority, dedup_key)
   ├─ advisory_xact_lock(chat_id, day)      # сериализация против гонки на границе потолка
   ├─ hard-gate предпочтений (opt-out)       # сейчас чокпоинт-заглушка (store впереди)
   ├─ дедуп по dedup_key в окне класса
   ├─ потолок: COUNT по notification_queue (chat, class, day, status IN queued/sent)
   └─ INSERT в notification_queue (status=queued) | suppressed (с reason, не молча)
        ↓
drain (cron, при DELIVERY_LAYER_ENABLED) → ORDER BY priority, FOR UPDATE SKIP LOCKED
   ├─ log-before-send: журнал в learning.domain_event + status=sent
   └─ deliver_fn(chat_id, content_spec) → транспорт (Bot.send_message)
```

## Инварианты

- **Контракт канало-агностичен:** `content_spec` без `chat_id`/`reply_markup`. Telegram-рендер — в `deliver_fn`/транспорте, не в очереди.
- **Очередь приватна:** raw SQL в `notification_queue` снаружи `core.notification_service` запрещён. Внешние платформенные producers (РП117) — через owned `enqueue`-контракт, не прямой INSERT.
- **Потолок по журналу/очереди, без квота-таблицы** (OwnerIntegrity).
- **Не молчать при подавлении:** suppressed пишется в очередь с `reason` + лог.

## Статус (Ф3)

- ✅ Ядро: `enqueue` (политика), `drain` (consumer), класс-модель, очередь, дедуп, потолок с advisory-lock, приоритет.
- ⏳ Выключено в проде (`DELIVERY_LAYER_ENABLED=false`) — пока 107 точек не мигрированы на `enqueue` (Ф4); иначе drain дублировал бы существующую доставку.
- ⏳ Заглушки/следующие шаги: предпочтения per-user (нет store), интеграция drain с retry транспорта DP.ROLE.044, мультиканальность через Apprise (Ф5).

## Тесты

`tests/smoke/test_notification_service.py` — потолок подавляет третий `capped`, `critical` обходит лимит, дедуп подавляет, очередь принимает, дренаж доставляет и помечает sent.
