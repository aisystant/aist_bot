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
   ├─ journal_key уже в журнале → suppressed: already-sent, строка НЕ создаётся
   │    (сбой чтения журнала → пропускаем дальше, дренаж проверит ещё раз; РП-568 Ф16 1б)
   ├─ advisory_xact_lock(delivery_lock_key)  # (chat, московский день); тот же ключ у nudge_delivery
   ├─ capped + заблокировал бота (user_state.bot_blocked) → suppressed: blocked
   │    (остальные классы пробуют: отметка снимается только когда человек напишет)
   ├─ hard-gate предпочтений (opt-out)       # сейчас чокпоинт-заглушка (store впереди)
   ├─ дедуп по dedup_key в окне класса (queued/sending/sent/unknown)
   ├─ потолок: COUNT по notification_queue (chat, class, московский день, queued/sending/sent/unknown)
   └─ INSERT в notification_queue (status=queued) | suppressed (с reason, не молча)
        ↓
drain (cron раз в минуту, при DELIVERY_LAYER_ENABLED)
   ├─ sending старше 10 мин → unknown (процесс упал посреди вызова)
   ├─ забрать пакет: короткая транзакция, FOR UPDATE SKIP LOCKED → status=sending, attempts+1
   └─ по каждой строке ВНЕ транзакции (сбой базы до вызова Telegram → строка снова queued;
        после вызова — остаётся sending → unknown, без повтора; не тронутые строки пакета → queued):
        ├─ ключ уже в журнале → suppressed: journal-dup
        ├─ deliver_fn(chat_id, content_spec) → рендер → Bot.send_message
        └─ исход (РП-568 Ф16 шаг 1а):
             вернула            → sent + журнал domain_event (только здесь) + receipt
             DeliveryBlocked    → blocked (бот заблокирован, отметка в user_state)
             DeliveryPermanent  → failed_permanent (400 и т.п., без повтора)
             DeliveryTransient  → queued через 2^attempts мин, ≤3 попыток, затем failed_permanent
             прочее (таймаут, сеть) → unknown: без повтора и без журнала
сторож _watch_delivery_queue (cron */10 мин, БЕЗ гейта флага)
   └─ queued старше 10 мин или unknown за 90 мин (по времени захвата) → ops-алерт ПРЯМОЙ отправкой в dev-канал (cooldown 1 ч)
```

**`unknown` — на разбор человеком.** Запрос мог дойти до Telegram, поэтому автоповтора нет (дубль хуже потери). Ручной повтор: `UPDATE development.notification_queue SET status='queued', scheduled_at=NOW() WHERE id=<id>`, только убедившись, что сообщение не пришло.

### Семантический журнал (Ф4, peer-сессия 2026-06-12-06)

Журнал пишется только после ответа Telegram «принято» (до РП-568 Ф16 — до отправки, и ошибка Telegram теряла сообщение молча). Readers журнала (`was_nudge_sent_recently` — cooldown нудж-правил;
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
**1б** ✅ `_send_practice_nudges` (+ `actions`-кнопка), `_send_slot_daily_prompt` (capped, ключ `slot_daily_prompt:{chat}:{московский день}`, пять кнопок столбиком; РП-568 Ф16 шаг 1б, 12.11) · **1в** ⏳ upgrade-нудж F/G
внутри `send_engagement_nudges` (rich CTA, зона РП349/406) · **2** ⏳ transactional
(oauth_server, tier_upgrade) · **3** ⏳ ops-alert (autofix/health → класс + canary) ·
**4** ⏳ must-deliver (марафон-контент, дайджесты — кнопки/pregen/catch-up, самая рискованная).

## Статус

- ✅ Ф3 — ядро: `enqueue` (политика), `drain` (consumer), класс-модель, очередь, дедуп, потолок с advisory-lock, приоритет.
- ✅ Ф4 волны 1+1б — нудж-отправители на `enqueue`; рендер `format`/`actions` в `deliver_fn`; семантический журнал (`journal_key`/`journal_type`); сторож очереди.
- ✅ Включено в проде (`DELIVERY_LAYER_ENABLED=true`, проверено 07.09.2026, см. `core/scheduler.py` у оплаты стажировки).
- ✅ РП-568 Ф16 шаг 1а — исходы доставки, повторы временных ошибок, журнал после отправки, блокировка до очереди, московские сутки; правило напоминаний о пропущенном чек-ине (`core/checkin_nudge.py`, ниже).
- ✅ РП-568 Ф16 шаг 1б, первая волна (12.11): журнал проверяется до очереди (повтор не плодит строк); напоминание о практике отбирается и по ключу последнего слота `marathon_practice_nudge:{chat}:{day}:150m`; приглашение к слоту через очередь; четыре прямые функции `handlers/tier_upgrade.nudge_*` без вызовов удалены. Непереведённых инициативных отправителей 45 (`INITIATIVE_PENDING_CEILING`).
- ✅ РП-568 Ф16 шаг 1б, вторая волна (13.11): повтор того же подавления (чат, класс, ключ, причина, сутки потолка) не добавляет строку — возвращается сегодняшняя с `deduped: true`; граница суток одна у потолка и аудита (`SAME_DELIVERY_DAY_SQL`). Напоминания идут через очередь: пользовательское (`/remind`) — `transactional`, ключ `user_remind:{chat}:{id}`; legacy +1h/+3h — `capped` с кнопкой урока, ключ `reminder:{chat}:{date}:{type}`; `journal_type=reminder`. Событие `reminder_delivered` пишет дренаж только после «принято» Telegram (поля из `content_spec.meta`). `check_reminders`: строка блокируется, ставится в очередь, затем `sent=TRUE` («обработано»: в очереди или окончательно подавлено); заблокировавший бота во время обхода пропускается до следующего вызова; при выключенном `DELIVERY_LAYER_ENABLED` напоминания ждут. Непереведённых 43.
- ⏳ Заглушки/следующие шаги: предпочтения per-user (нет store), мультиканальность через Apprise (Ф5). Поле `format` — компромисс одного канала; при Ф5 пересмотр на структурированные блоки.

## Напоминания о пропущенном чек-ине (РП-568 Ф16, DP.SC.116)

Правило пилота 11.11.2026: об одном пропуске — не больше двух напоминаний: на следующий день и через три дня после первого, дальше тишина до возвращения.

- Эпизод M = первый пропущенный рабочий день после последней активности: `learning.marathon_activity` (любой `action_type`; снятие паузы пишет `resume` на сегодня — дни на паузе не пропуски) плюс живые чек-ины из `learning.marathon_state` со вчерашнего дня (ночной подсчёт видит только вчера; без них отметившийся сегодня утром получил бы «вчера не получилось»).
- Ступень 1 — в любой день окна [M+1, M+3]; ступень 2 — в окне [S+3, S+5], где S — московская дата фактической доставки ступени 1 (из журнала); без ступени 1 ступени 2 нет.
- Ключ журнала `marathon_nudge:{chat}:{M}:{1|2}` — каждая ступень один раз за эпизод. Напоминание старого формата (`marathon_nudge:{chat}:{дата}`) внутри эпизода считается ступенью 1 — иначе первый запуск после выкатки повторил бы его.
- Нет отметки ночного подсчёта активности за вчера (`marathon_activity_batch` в `domain_event`) → никому не шлём, одна строка в dev-канал за день.
- Класс `capped`: если потолок 2/день занят напоминаниями о практике, ступень уйдёт на следующий день окна.
- Выбор участников — `get_checkin_episodes(today)`, без `LIMIT`.

## Тесты

`tests/smoke/test_notification_service.py` — потолок подавляет третий `capped`, `critical` обходит лимит, дедуп подавляет, блокировка отсекает до очереди, очередь принимает; дренаж: каждый исход (sent/blocked/failed_permanent/повтор с отсрочкой/исчерпание попыток/unknown/сбой проверки журнала), журнал только после доставки — семантический ключ/тип + fallback.
`tests/smoke/test_slot_daily_prompt.py` — приглашение к слоту идёт в `enqueue` классом `capped` с дневным ключом и пятью кнопками, сбой одного не останавливает остальных.
`tests/test_checkin_nudge.py` — сценарии правила чек-ина (пн → вт и пт, пропущенный запуск, поздняя первая ступень, тишина, нет ночного подсчёта).
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
