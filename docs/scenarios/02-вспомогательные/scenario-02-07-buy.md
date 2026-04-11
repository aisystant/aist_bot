# 02.07 `/buy` — покупка курсов и подписки

> Единый storefront для покупки курсов и подписок Aisystant. Inline-callback wizard.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/buy` |
| Вид | Вспомогательная (B) — inline callback wizard |
| Файл | [`handlers/buy.py:36`](../../../handlers/buy.py) |
| FSM | Нет (inline callbacks через `edit_text`) |
| Платежи | Yookassa (pre-created URL) + Aisystant API fallback |

---

## 1. Flow

```
/buy
  ↓
Showcase (приветствие + категории)
  ↓
Catalog (список курсов из Aisystant)
  ├─ Выбор курса
  ↓
Payment options
  ├─ Full payment → Yookassa URL
  └─ Installment (35% first) → Yookassa split URL
```

## 2. Правила

- **Installment split:** для курсов ≥ 35 000₽ предлагается оплата 35% сейчас + 65% потом. URL pre-creation через Yookassa API.
- **Fallback:** если pre-creation не удался — callback `_create_payment_url` при нажатии кнопки (lazy).
- **`/buy` vs `/schedule`:** `/buy` — storefront, `/schedule` — навигация по курсам по категориям.

## 3. Источники

| Что | Откуда |
|-----|--------|
| Курсы | `clients/aisystant.get_available_courses()` |
| Цены и валюта | API Aisystant |
| Payment URL | Yookassa pre-creation |
| Кеш | Neon (курсов и категорий) |

## 4. Tier-логика

- **T0-T1:** показывает курсы с paywall «Привяжите Aisystant»
- **T2+:** полный доступ к покупкам, history в `subscriptions`

## 5. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/buy.py` | `cmd_buy` + inline callbacks |
| `clients/aisystant.py` | LMS API |
| `clients/yookassa.py` | Payment pre-creation |

## 6. Связанные таблицы

- `subscriptions` — история подписок (Telegram Stars + Aisystant)
- `conversion_events` — `tier_upgrade_shown`, click/dismiss

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
