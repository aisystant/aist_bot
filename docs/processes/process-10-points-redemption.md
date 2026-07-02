# 10. Зачёт баллов в оплату (WP-327)

> **Pack source-of-truth:** [DP.SC.141 «Зачёт баллов в оплату»](../../../../PACK-digital-platform/pack/digital-platform/08-service-clauses/DP.SC.141-points-redemption.md) + [DP.ROLE.051 «Points Redeemer»](../../../../PACK-digital-platform/pack/digital-platform/02-domain-entities/DP.ROLE.051-points-redeemer.md).
>
> Поток позволяет пилоту применить накопленные бонусы как скидку при оплате семинара (workshop_seminar или showcase product). Двухфазный коммит: резерв при чекауте → подтверждение по webhook (YK) или successful_payment (TG Stars).

---

## 1. Точки интеграции в коде

| Файл | Шаг |
|------|-----|
| `helpers/redeem_helpers.py` | `prepare_burn_offer`, `reserve_for_yookassa`, `reserve_for_tg_stars`, UI-формирование |
| `db/queries/redeem.py` | `available_discount`, `reserve_burn`, `confirm_burn`, `rollback_burn`, `rollback_expired_reservations` |
| `handlers/workshop.py` | callbacks `seminar_iwe_pay_rub*`, `seminar_iwe_pay*` (burn-варианты) + `on_workshop_payment` (Stars confirm) + `process_yookassa_webhook` (YK confirm) |
| `handlers/showcase.py` | callbacks `showcase_pay_rub*`, `showcase_pay_stars*` (burn-варианты) + `on_seminar_payment` (Stars confirm) + `process_seminar_yookassa_webhook` (YK confirm) |
| `core/scheduler.py` | cron `_rollback_expired_burn_reservations` каждые 5 мин |

---

## 2. UX-flow

```
Пилот → «Оплатить рублями» (workshop или showcase)
   ↓
prepare_burn_offer(chat_id, full_amount)
   ↓
discount_rub > 0 ?
   ├─ нет → старый flow: YK create_payment с полной ценой
   └─ да  → показать UI: «У вас N бонусов = X₽ скидки. Доплата Y₽. Применить?»
              ├─ «Применить»  → callback *_burn  → reserve_burn + create_payment(payable_rub)
              └─ «Без скидки» → callback *_full  → create_payment(full_amount)
```

> **Терминология (DP.ECON.001 §1.1, DP.D.050):**
> - **Баллы** = earned_total — геймификация, никогда не убывают, видны в лидерборде
> - **Бонусы** = `min(point_balances.points, Σ(active_days_at_qual_i × daily_cap_i))` — скидка при оплате, тратятся, ограничены историей активных дней × cap квалификации
> - В UI пользователю показываем «бонусы», не «баллы» (баллы — это то, что накоплено всего)

**TG Stars:** аналогично, но резерв с `provisional_id = "tg_{uuid4}"` ДО `create_invoice_link` (т.к. `telegram_payment_charge_id` известен только после оплаты). `provisional_id` встроен в `invoice_payload` (`..._p_{provisional_id}`).

---

## 3. Confirm-точки

| Канал | Где вызывается `confirm_burn(payment_id)` |
|-------|-------------------------------------------|
| YooKassa (workshop) | `handlers/workshop.process_yookassa_webhook` после `create_and_confirm_payment` (`payment_id` = YK ID) |
| YooKassa (showcase) | `handlers/showcase.process_seminar_yookassa_webhook` после `create_seminar_payment` |
| TG Stars (workshop) | `handlers/workshop.on_workshop_payment` парсит `provisional_id` из payload (`_p_` суффикс) |
| TG Stars (showcase) | `handlers/showcase.on_seminar_payment` аналогично |

**Идемпотентность:** `confirm_burn` no-op если резерва не было (для пилотов без баллов) и идемпотентна при дублирующих webhook (status='confirmed' → True).

---

## 4. Race condition: reserve после YK create_payment

YK `create_payment` создаёт pending-платёж (не списывает с карты). Window между create и reserve — миллисекунды. Если в этом окне другой handler того же пилота тоже резервирует баллы, `reserve_burn` вернёт `False` (баланс упал).

**Поведение при race:** перевыпускаем YK-платёж на полную сумму через `create_payment(amount=full_amount)`. Старый pending-платёж пилот не увидит (показываем новую ссылку), он истечёт по таймауту ЮКассы (~1 сутки). Скидка не применяется.

---

## 5. Cron rollback

`rollback_expired_reservations` (db/queries/redeem.py) переводит резервы старше 30 минут в `status='rolled_back', rollback_reason='timeout_30min'` и эмитирует `points_burn_rolled_back` event. Запуск каждые 5 минут через `core/scheduler._rollback_expired_burn_reservations`.

---

## 6. Late-webhook сценарий

Если YK webhook приходит после `rollback_expired_reservations` (пилот реально оплатил, а резерв уже откатился) — `confirm_burn` возвращает `False` и эмитирует `points_redeem_late_webhook` event для admin-разбора (см. `db/queries/redeem.confirm_burn` § rolled_back branch). Оплата записывается как обычно, баланс баллов не трогается → требует ручного refund через `point_balances` adjust.

---

## 7. Связанные источники

- Source-of-truth контракт: [DP.SC.141](../../../../PACK-digital-platform/pack/digital-platform/08-service-clauses/DP.SC.141-points-redemption.md)
- Роль исполнителя: [DP.ROLE.051](../../../../PACK-digital-platform/pack/digital-platform/02-domain-entities/DP.ROLE.051-points-redeemer.md)

---

## 8. Курсовые резервы: проактивное подтверждение (WP-446 Ф3b)

Курсовые оплаты (`purpose='COURSE'`) идут через Aisystant, а не через YK webhook этого бота — раздел «Confirm-точки» (§3) к ним не относится. Их подтверждает `db/queries/redeem.confirm_course_reserves` (lazy, при входе в «Мои программы») и, с 2026-07-02, дополнительно `core/scheduler._confirm_pending_course_payments` (cron каждые 10 мин): опрашивает `clients.aisystant.check_payment` для резервов, уже промоутнутых к реальному `payment_id`, и подтверждает только на `SUCCEEDED` (`FAILED`/`IN_PROGRESS` — no-op, окончательную очистку делает §5).

**Связь с §5:** `rollback_expired_reservations` (§5) и `confirm_reserve_by_payment_id` (новая) сериализованы через `pg_advisory_xact_lock(hashtext('burn_reserve:' || payment_id))` — иначе TTL-откат мог бы откатить резерв в момент, когда Aisystant уже сообщил `SUCCEEDED` (доступ к курсу выдан, бонусы не списаны).
- Миграция схемы: `DS-IT-systems/neon-migrations/mvp/226-wp327-rewards-redeemed-events.sql`
- Курс конвертации: `POINTS_TO_RUB_RATE = 0.875 ₽/балл` в `db/queries/redeem.py` (источник: DP.SC.105 = `$0.01 × курс USD/RUB`)
