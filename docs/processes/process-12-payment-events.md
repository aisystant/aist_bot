# Процесс 12. События оплаты и welcome-бонус (WP-266 Ф5c)

> Категория: Процесс (внутренняя логика, не экран). Код: `helpers/dual_write.py`
> (`emit_payment_received`), врезки в `handlers/workshop.py`, `handlers/showcase.py`,
> `handlers/subscription_stars.py` (исторически первая точка).

## Что делает

Каждый канал оплаты после подтверждения платежа эмитит **сырое** событие
`payment_received` в event-gateway. Канал НЕ решает, первая ли это оплата —
в боте нет единой таблицы платежей (workshop_payments / seminar_payments /
Stars-подписки фрагментированы по telegram_id).

Дальше по конвейеру:

1. event-gateway валидирует по строгой схеме `payment_received.v1`
   (required: payment_id, amount, currency, payment_kind_code,
   external_payment_id, provider, paid_at; additionalProperties: false).
2. projection-worker правилом 104 кладёт строку в `payment.payment_received`
   (идемпотентность: UNIQUE(source, external_ref)).
3. Hook воркера (`first_payment.py`) на том же событии для домена rewards:
   guard-таблица `rewards.first_payment_guard` (один welcome на account_id
   за всю жизнь) → welcome плательщику + бонус пригласившему через
   `compute_effective_amount_v4` (суммы по правилам `first_payment_detected`
   и `referral_attributed` в reference, эффективная сумма зависит от
   уровня и капов). Атрибуция реферала — `learning.onboarding_state.referral_source`.

## Точки эмиссии (5)

| Канал | Файл | provider | payment_kind_code |
|-------|------|----------|-------------------|
| Подписка Stars | `subscription_stars.py` | tg_stars | stars |
| Подписка YooKassa (webhook) | `workshop.py:process_yookassa_webhook` | yookassa | bank_card |
| Мастерская Stars | `workshop.py:on_workshop_payment` | tg_stars | stars |
| Мастерская Aisystant (webhook) | `workshop.py:process_workshop_webhook` | aisystant | manual |
| Семинар Stars | `showcase.py:on_seminar_payment` | tg_stars | stars |
| Семинар YooKassa (webhook) | `showcase.py:process_seminar_yookassa_webhook` | yookassa | bank_card |
| Семинар Aisystant/Tilda (webhook) | `showcase.py:process_seminar_aisystant_webhook` | aisystant | manual |

## Ограничения (зафиксированы review 2026-06-12)

- Без `external_payment_id` или с `amount <= 0` эмиссия пропускается
  с warning (идемпотентный ключ невозможен / CHECK(amount>0) в payment БД).
- Telegram-карта (`currency != XTR` в successful_payment) не эмитится:
  провайдер вне enum схемы шлюза. Такой платёж не участвует в welcome,
  global-first сместится на следующий платёж — расширение enum = отдельная задача.
- Событие `subscription_first_purchased` — supersede (WP-327 Этап 22 → WP-266 Ф5c):
  эмиссия убрана, правило закрыто миграцией 264.

## 4. Надёжность Stars-платежей: транзакционный outbox (WP-567 Ф3в)

> Код: `handlers/subscription_stars.py` (`on_successful_stars_sub`), `db/queries/event_outbox.py`, `db/queries/subscription.py` (`save_subscription_with_outbox`), дренаж/сторож — `core/scheduler.py` (`_drain_event_outbox`, `_watch_event_outbox`). Таблица → [tables.md §6.5a `event_outbox`](../data/tables.md).
>
> **Вне объёма:** `handlers/payments.py` (донаты, разовые и recurring) вызывает голый `save_subscription()`/`upsert_subscription_grant()`, НЕ `save_subscription_with_outbox` — этот путь outbox не защищён, `subscription_granted` там по-прежнему уходит через старый fire-and-forget `post_event`, `payment_received` не эмитится вовсе. Сужение объёма было сознательным решением сессии (см. ниже), но именно этот файл в него не попал.

**Проблема (найдена при разборе, не совпала с исходной постановкой задачи):** обработчик Stars-оплаты не пишет баллы напрямую и не теряет их при недоступности БД баллов, как предполагала первая формулировка задачи — у отдельного `multi-domain-projection-worker` уже есть свой курсор + DLQ + backoff retry для `learning.domain_event`. Два реальных разрыва были в другом:

1. Событие эмитилось через `asyncio.create_task(post_event(...))` — честный fire-and-forget HTTP-вызов без собственной надёжности. Если процесс бота умирал между постановкой задачи и её фактическим выполнением (OOM на Railway, деплой, стопор event loop под нагрузкой) — событие терялось без следа.
2. `save_subscription()` вызывался в голом `try/except: logger.warning(...)` без re-raise. `public.subscriptions` — не одноразовый FSM-маркер, как называли устаревшие комментарии в коде, а durable-запись, используемая `get_active_subscription`/`cancel_subscription`. Потеря этого INSERT теряла подписку пользователя уже после списания звёзд.

**Решение:** `on_successful_stars_sub` — одна транзакция, вставляющая строку подписки И обе строки outbox, либо ничего из них. Отдельный дренаж (`_drain_event_outbox`, cron `*/1мин`) вычитывает недоставленные строки `FOR UPDATE SKIP LOCKED` (тот же паттерн, что `core/notification_service.py` `drain()`) и шлёт их в event-gateway; savepoint на строку — ошибка одной строки не откатывает уже успешно отправленные строки того же батча. Сторож (`_watch_event_outbox`, `*/10мин`) алертит разработчика, если что-то висит недоставленным >10 мин, fail-open по своей же ошибке БД.

**Сознательное сужение объёма:** только `payment_received`/`subscription_granted` от Stars-платежей — не общая политика outbox для всех событий бота (Kimi сузил первоначальное предложение Codex, пир-сессия `2026-09-12-09-wp567-stars-retry-parnaya-zapis`). Остальные события бота продолжают идти через существующий fire-and-forget `post_event`.

**Идемпотентность:** `external_id` в `event_outbox` — тот же ключ, что уже уходит в конверт event-gateway для каждого события (не новый составной ключ) — `ON CONFLICT (external_id) DO NOTHING` на вставке.

### Подтверждение доставки и аварийные уведомления (WP-562 / WP-567)

Дренаж использует строгий `post_event_or_raise`: отметка доставки допустима
только после ответа шлюза `201 {inserted: true, id: <непустая строка>}` либо
`200 {inserted: false, idempotent: true}`. Отключённый `EVENT_GATEWAY_ENABLED`,
перенаправление, неизвестный статус или некорректное подтверждение оставляют
событие для повтора. После неоднозначного тайм-аута повтор идёт с прежним
`external_id`; отмена задачи не считается подтверждением. Сырой ответ шлюза
не записывается в новые сообщения об ошибке. Старый `post_event` сохраняет
свою модель доставки без гарантированного повтора.

Несохранённый recurring-донат, Stars-подписка после трёх неудачных попыток
и зависшие события сообщаются через `core/operator_alerts.py`. Этот узкий
канал не использует БД или очередь обычных уведомлений, отправляет только
на `DEVELOPER_CHAT_ID` и ограничивает ожидание Telegram пятью секундами.
Исключение в проверке прямых отправителей дано только его функции транспорта;
обработчики платежей остаются под общим запретом. Сбой чтения языка пользователя
не прерывает обработку уже оплаченного доната: применяется русский язык.

При отказе алерта пользователь всё равно получает сообщение о задержке
обработки. В алерте остаются необходимые идентификаторы для восстановления,
но нет сырого текста исключения. Доставка оператору не гарантирована:
неуказанный адресат или недоступный Telegram дают явную ошибку в журнале;
после неоднозначного тайм-аута или перезапуска возможен повтор. Сторож подавляет
повторные сообщения на час только после подтверждённой отправки.

Эти условия проверяют `tests/smoke/test_operator_alerts.py` и
`tests/smoke/test_event_outbox_strict_delivery.py` в обязательном профиле
проверок кандидата P-18. Тесты используют искусственные данные и не доказывают
живую отправку, применение миграции или завершение РП-562.

## Источники

- Контекст РП: `DS-my-strategy/inbox/WP-266-guest-pass-concept.md` § Ф5c
- Peer-sessions: 2026-06-11-39 (архитектура), 2026-06-12-03 (стройка); WP-567 Ф3в — 2026-09-12-09-wp567-stars-retry-parnaya-zapis (Claude+Kimi+Codex)
- Миграции: neon-migrations mvp/263, 264, 265 + scripts/backfill-first-payment-welcome.py; `db/migrations/049_wp567_event_outbox.py` (WP-567 Ф3в, DATABASE_URL основной БД бота — не Neon rewards)
