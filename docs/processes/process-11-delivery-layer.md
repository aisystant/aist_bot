# Процесс 11 — Слой доставки (Доставщик)

> **Pack:** see DP.SC.177 (обещание), DP.ROLE.075 (роль). **РП:** WP-418 Ф3-Ф4.
> **Код:** `core/notification_service.py`, очередь в `db/models.py` (`notification_queue`), дренаж и сторож в `core/scheduler.py` (`_drain_delivery_queue`, `_watch_delivery_queue`).
> **Флаг:** `DELIVERY_LAYER_ENABLED` (по умолчанию `false`; порядок включения — см. «Порядок выкатки волны»).

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
in-bot отправитель → enqueue(chat_id, klass, content_spec, priority, dedup_key,
                              journal_key, journal_type)
   ├─ advisory_xact_lock(chat_id, day)      # сериализация против гонки на границе потолка
   ├─ hard-gate предпочтений (opt-out)       # сейчас чокпоинт-заглушка (store впереди)
   ├─ дедуп по dedup_key в окне класса
   ├─ потолок: COUNT по notification_queue (chat, class, day, status IN queued/sent)
   └─ INSERT в notification_queue (status=queued) | suppressed (с reason, не молча)
        ↓
drain (cron, при DELIVERY_LAYER_ENABLED) → ORDER BY priority, FOR UPDATE SKIP LOCKED
   ├─ log-before-send: журнал в learning.domain_event + status=sent
   │    external_id = journal_key (семантический, от отправителя) | delivery:{id} (fallback)
   │    notification_type = journal_type ('nudge', 'marathon_nudge', ...) | klass (fallback)
   └─ deliver_fn(chat_id, content_spec) → рендер (_build_delivery_kwargs) → Bot.send_message
        ↑ format: markdown→HTML (md_to_html) | html | plain; actions → InlineKeyboard
сторож _watch_delivery_queue (cron */10 мин, БЕЗ гейта флага)
   └─ queued старше 10 мин → ops-алерт ПРЯМОЙ отправкой в dev-канал (cooldown 1 ч, fail-open)
```

### Семантический журнал (Ф4, peer-сессия 2026-06-12-06)

Readers журнала (`was_nudge_sent_recently` — cooldown нудж-правил;
`get_notification_stats`, `notification_engagement` → ЦД) контрактно зависят от
`external_id = "notification-{прежний idempotency_key}"` и `payload.notification_type`
с прежними семантическими типами. Поэтому при миграции отправителя его прежний
ключ/тип передаются в `enqueue` как `journal_key`/`journal_type` — журнал пишет
ТОЛЬКО drain (один writer, без двойного счёта в аналитике), readers не меняются.
Удалить запись отправителя «просто так» нельзя было: cooldown ослеп бы.

### Порядок выкатки волны миграции

1. `DELIVERY_LAYER_ENABLED=true` в Railway **ДО** merge — для кода без этой ветки env no-op.
2. Merge + деплой → drain активен с первой секунды, окна «очередь копится» нет.
3. Canary: enqueue тестового `ops-alert` в dev-канал → проверить цикл queue → drain → журнал → рендер.
4. Сторож страхует от сброса env при будущих редеплоях (алерт ≤10 мин).

## Инварианты

- **Контракт канало-агностичен:** `content_spec` без `chat_id`/`reply_markup`. Telegram-рендер — в `deliver_fn`/транспорте, не в очереди.
- **Очередь приватна:** raw SQL в `notification_queue` снаружи `core.notification_service` запрещён. Внешние платформенные producers (РП117) — через owned `enqueue`-контракт, не прямой INSERT.
- **Потолок по журналу/очереди, без квота-таблицы** (OwnerIntegrity).
- **Не молчать при подавлении:** suppressed пишется в очередь с `reason` + лог.

## Scope миграции (Ф4)

Критерий: **отправка не является ответом на входящее событие текущего хода
пользователя → enqueue**. Реактивные ответы FSM/handlers (пользователь нажал →
бот ответил) в очередь не идут: лимит на них бессмыслен, минутный дренаж ломает
диалог. Ограничение обещания: потолок DP.SC.177 не покрывает диалоговые серии.

Волны: **1** ✅ `_send_marathon_nudges`, `send_engagement_nudges` (текст) ·
**1б** ✅ `_send_practice_nudges` (+ `actions`-кнопка) · **1в** ⏳ upgrade-нудж F/G
внутри `send_engagement_nudges` (rich CTA, зона РП349/406) · **2** ⏳ transactional
(oauth_server, tier_upgrade) · **3** ⏳ ops-alert (autofix/health → класс + canary) ·
**4** ⏳ must-deliver (марафон-контент, дайджесты — кнопки/pregen/catch-up, самая рискованная).

## Статус

- ✅ Ф3 — ядро: `enqueue` (политика), `drain` (consumer), класс-модель, очередь, дедуп, потолок с advisory-lock, приоритет.
- ✅ Ф4 волны 1+1б — нудж-отправители на `enqueue`; рендер `format`/`actions` в `deliver_fn`; семантический журнал (`journal_key`/`journal_type`); сторож очереди.
- ⏳ Выключено в проде (`DELIVERY_LAYER_ENABLED=false`) — включать по «Порядку выкатки» выше.
- ⏳ Заглушки/следующие шаги: предпочтения per-user (нет store), интеграция drain с retry транспорта DP.ROLE.044, мультиканальность через Apprise (Ф5). Поле `format` — компромисс одного канала; при Ф5 пересмотр на структурированные блоки.

## Тесты

`tests/smoke/test_notification_service.py` — потолок подавляет третий `capped`, `critical` обходит лимит, дедуп подавляет, очередь принимает, дренаж доставляет и помечает sent; журнал — семантический ключ/тип + fallback.
`tests/smoke/test_delivery_wave1.py` — рендер markdown→HTML/plain/actions; сторож fail-open; инвариант «в мигрированных функциях нет прямых `bot.send_*`, есть `enqueue`».

## Второй движок — nudge-политика для WP-117 (Ф-decouple, 2026-07-04)

> **Код:** `core/nudge_delivery.py`. **РП:** WP-418 (deliverables) + WP-117 Ф-decouple (contract `DS-my-strategy/inbox/WP-117/f-decouple-contract.md`).

WP-117 переходит от бот-планировщика к платформенному producer'у нуджей (`NudgeCandidate`). Для него — **отдельный, самостоятельный движок политики**, не обёртка над `enqueue()`/`CLASS_POLICY`: у нового producer'а политика ключуется по `nudge_type` (`NUDGE_TYPE_CONFIG`), а не по 5-классовой модели — прогон через `enqueue(klass=...)` применил бы ЧУЖОЙ cooldown/cap для этого типа, дав тихий policy-конфликт (peer-session 2026-07-04-11).

**Разделяет с основным движком:** таблицу `notification_queue` и — для типов с `class_cap=capped` — тот же бакет `notification_class='capped'` и **тот же buквальный advisory-lock ключ** `deliver:{chat_id}:{day}`, что `enqueue()` (иначе движки не видят гонку друг друга — найдено и исправлено при code review этой сессии). Единый честный счётчик DP.SC.177 остаётся один на все 65+N отправителей.

**Интерфейсы (§2.2 контракта):**
- `NUDGE_TYPE_CONFIG: dict[str, NudgeTypeConfig]` — пусто, наполняется WP-117 по мере переноса правил.
- `select_and_enqueue(candidates) -> list[EnqueueResult]` — cooldown (dedup_key в очереди) → class_cap (`capped` делит бакет с legacy; `any` независим; `exclusive` преемптит остальных кандидатов пользователя в батче) → opt-out (чокпоинт-заглушка, как у `enqueue()`) → INSERT.
- `get_recent_nudges_batch(user_ids, nudge_types)` — история для state-predicate producer'а, один SQL-запрос на тип (`db.queries.notifications.fetch_recent_nudges_by_type`).

**Тесты:** `tests/smoke/test_nudge_delivery.py` — 10 сценариев (unknown_type, cap, cooldown, exclusive-preemption, batch-группировка, get_recent_nudges_batch фильтрация/reshape).

**Статус:** контракт реализован, `NUDGE_TYPE_CONFIG` пуст — реального трафика через этот путь нет, пока WP-117 не подключит producer.
