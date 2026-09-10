---
family: C
type: scenario
commands: []
tier_access: T0-T4
status: active
wp: WP-181
related_sc: null
---

# 03.35 Прямая оплата Мастерской IWE (deep-link)

> `/start masterskaya_direct` — карточка оплаты Мастерской IWE в один платёж,
> без прохождения Семинара. Для ссылок из внешнего Telegram-канала и лендинга.

---

## Поток

```
[Ссылка t.me/aist_me_bot?start=masterskaya_direct]
     │
     ↓ /start masterskaya_direct
     │
     ↓ has_direct_masterskaya_payment(chat_id) или count >= 2?
     │        да → «У вас уже есть доступ» (конец)
     │        нет ↓
     ↓ Карточка «Мастерская IWE — 8000₽»
       [💳 Оплатить картой]   callback: direct_masterskaya_pay_rub
       [⭐ Оплатить звёздами]  callback: direct_masterskaya_pay_stars
     │
     ├─ картой → ЮКасса create_payment (metadata.purpose=WORKSHOP_DIRECT)
     │            ИЛИ Aisystant-лендинг → webhook purpose=WORKSHOP_DIRECT
     │
     └─ звёздами → create_invoice_link (payload workshop_direct_{chat_id})
                    → successful_payment → log_action в Aisystant (запись, не платёж)
     │
     ↓ workshop_payments.product = 'masterskaya_direct'
     │
     ↓ invite в MASTERSKAYA_IWE_CHAT_ID (creates_join_request=True)
     │
     ↓ chat_join_request → approve (count>=2 ИЛИ has_direct_masterskaya_payment)
```

## Отличие от обычной воронки (Семинар → Мастерская)

Обычная воронка (`sched_workshop`) требует двух последовательных оплат
(count по `workshop_payments`). Прямая покупка — отдельная явная метка
`product='masterskaya_direct'` в той же таблице, т.к. `amount` не годится
как признак товара (Stars и рубли пишут разные шкалы чисел за один и тот
же товар: 4000⭐ vs 8000₽).

## Оплата звёздами → запись в Aisystant

Деньги-звёзды не покидают Telegram технически. `_notify_aisystant_stars_payment()`
шлёт в Aisystant (`clients/aisystant.py::log_action`, `/tg/log`) запись о факте
оплаты (не платёж) с рублёвым эквивалентом 8000₽ — для учёта на стороне Aisystant.
`/tg/log` ранее в коде не использовался ни разу — контракт не проверен реальным
вызовом до первого платежа в проде.

## Режим отказа

- ЮКасса credentials отсутствуют → `workshop.pay_error`, ссылка не создаётся
- create_chat_invite_link падает → `workshop.invite_error`, ручное добавление через @ssm_tg
- `log_action` в Aisystant падает → залогировать, доступ в чат Мастерской всё равно выдать (учёт вторичен по отношению к доступу)

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-09-10 | WP-181 Ф-direct: создан. handlers/workshop.py show_direct_masterskaya_card + callback_direct_masterskaya_pay_rub/stars, миграция 046 (workshop_payments.product). |
