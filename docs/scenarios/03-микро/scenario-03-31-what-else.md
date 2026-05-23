---
family: C
type: scenario
commands: []
tier_access: T1-T4
status: active
wp: WP-349
related_sc: DP.SC.156
---

# 03.31 «💡 Что ещё?» — обнаружение возможностей уровня

> Кнопка на tier-экране /setup. Показывает список команд текущего уровня.
> Tier-aware: T1 видит только T1-команды, T2+ свои. DP.SC.156.

---

## Поток

```
[Tier-экран /setup]
     │
     ↓ нажать «💡 Что ещё?»     callback: setup:what_else
     │
     ↓ detect_ui_tier(user_id)
     │
     ↓ Экран «Что ещё?»
       💡 Что ещё доступно на твоём уровне:
       • /support — поддержка          (T1)
       • /points — баллы               (T1)
       • /remind — ежедневное напоминание (T1)

       [← Назад]  callback: setup:tier_back
     │
     ↓ нажать «← Назад»
     │
     ↓ delete() + send_setup_screen()
     │
     [Tier-экран снова]
```

## Tier-aware контент

| Уровень | Команды в «Что ещё?» |
|---------|----------------------|
| T1 «Старт» | /support, /points, /remind |
| T2 «Изучение» | /knowledge, /progress, /rp |
| T3 «Персонализация» | /twin, /me, /navigator |
| T4 «Созидание» | /plan, /knowledge_post, /week_close |

## Режим отказа

- domain_event недоступен → показать экран без записи (fail-open)
- Неизвестный тир → кнопка не показывается (скрыть в `_tier_keyboard`)
- T0 → кнопка не показывается (T0 не видит tier-экран)

## Метрика

`event_type='what_else_opened'` в `learning.domain_event` с `payload.tier`.
CTR = count(what_else_opened) / count(setup_viewed) за 7 дней ≥ 5%.

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-05-23 | WP-349 Ф14: создан. handlers/setup.py on_what_else + on_tier_back. |
