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

## Источники

- Контекст РП: `DS-my-strategy/inbox/WP-266-guest-pass-concept.md` § Ф5c
- Peer-sessions: 2026-06-11-39 (архитектура), 2026-06-12-03 (стройка)
- Миграции: neon-migrations mvp/263, 264, 265 + scripts/backfill-first-payment-welcome.py
