# 02.11 `/subscription` — управление подпиской

> Просмотр тарифов, оформление новой / продление существующей подписки.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/subscription` |
| Вид | Вспомогательная (B) — inline callback wizard |
| Файл | [`handlers/subscription.py:102`](../../../handlers/subscription.py) |
| FSM | Нет (callbacks через `edit_text`) |
| Таблица | `subscriptions` |

---

## 1. Flow

```
/subscription
  ↓
Tariff list (inline kb)
  ├─ Месячная
  ├─ 3 месяца
  ├─ 6 месяцев
  └─ Годовая
  ↓
Period selected
  ├─ Показ цены и условий
  └─ [Оплатить] → Yookassa URL (pre-created)
  ↓
Payment result (callback)
```

## 2. Dual-purpose

Одна команда обслуживает два сценария:
- **Новая подписка** — user без активной записи в `subscriptions`
- **Продление** — показывает текущую дату истечения + кнопка продления

## 3. Источники

| Что | Откуда |
|-----|--------|
| Тарифы | `clients/aisystant.get_subscription_tariffs()` |
| Payment URL | `clients/aisystant.create_subscription_payment(period, amount)` |
| Кеш | Neon (цены и периоды) |
| PERIOD_LABELS | Dict в модуле (RU/EN локализация) |

## 4. Tier upgrade

После успешной оплаты:
- `subscriptions` INSERT (`stars_amount`, `started_at`, `expires_at`)
- `detect_ui_tier()` при следующем вызове → T1 → T2
- Event `tier_change` → `tier_events` INSERT
- Conversion event `subscription_purchased` → `conversion_events`

## 5. Telegram Stars vs Aisystant payment

⚠️ **Два независимых платежных канала:**
- **Aisystant payment** (Yookassa) — основной способ для «Бесконечное развитие», идёт через `clients/aisystant.py`
- **Telegram Stars** — используется для донатов / благодарностей (НЕ влияет на tier, только `is_first_recurring` в `subscriptions`)

Правило (§12 CLAUDE.md): TG Stars = донаты, НЕ влияют на tier/доступ.

## 6. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/subscription.py` | `cmd_subscription` + callbacks |
| `clients/aisystant.py` | API тарифов и payment URL |
| `db/queries/subscriptions.py` | CRUD |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
