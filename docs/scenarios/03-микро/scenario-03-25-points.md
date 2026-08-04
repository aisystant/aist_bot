# 03.25 Сценарий просмотра баллов

> Описание команд `/points` и `/rules`. WP-327 Phase 2 UX refactor (23 мая 2026).

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команды | `/points`, `/rules` |
| Тип | Микро-сценарий (вид C): один экран, один ответ |
| Источник | Neon БД `rewards` + `reference` (read-only) |
| Связь | WP-327 (Зачётчик), WP-306, DP.SC.122, DP.ECON.001 §1.1 |

---

## /points — баланс баллов

### Что показывает

1. **🪙 Заработано всего** (`earned_total`) — монотонный счётчик, никогда не убывает.
   Вычисляется: `point_balances.points + SUM(redeemed_events.points_amount WHERE confirmed)`.
2. **💎 Доступно бонусов** (`point_balances.points`) — можно потратить при оплате. 1 бонус = 0,10 ₽.
3. **📅 Сегодня** — сумма начислений за текущий день с прогресс-баром относительно суточного потолка.
4. **Последние 5 начислений** — компактно: `• Коммит в git +35 · 5 мин назад`.

### Сообщение бота

```
🏆 Ваши баллы

🪙 Заработано всего: 157 870
💎 Доступно бонусов: 157 170

📅 Сегодня: +200 ▓▓▓▓▓▓░░░░ 200/300 (67%)

Последние начисления:
• Коммит в git +105 · 3 ч назад
• Сессия в IWE +30 · 3 ч назад
• Коммит в git +35 · 3 ч назад
• Слот саморазвития +30 · 1 дн назад
• Помодоро +20 · 1 дн назад

1 бонус = 0,10 ₽ при оплате · /rules — как копить быстрее
```

### Граничные случаи

| Условие | Поведение |
|---------|-----------|
| Не привязан Aisystant (`dt_user_id = None`) | «Аккаунт не привязан. /settings» |
| Нет начислений (новый пользователь) | Текст с подсказками: /day_close, уроки, слоты |
| Нет данных о суточном потолке | Сегодня показывается без прогресс-бара |
| Ошибка БД | `t('errors.processing_error', lang)` |
| HTML render error | Fallback без тегов (CLAUDE.md §10.2) |

---

## /rules — правила начисления

### Что показывает

1. **Действия по группам** (Учёба / Практика и ритм / Работа) — из `reference.reward_rules`.
2. **Множители домена** — из `reference.activity_domain_multipliers` (learning ×3.0, practice ×5.0, work ×1.0).
3. **Ступени Ученика (1-5)** — из `reference.student_stage_multipliers` (name, multiplier, daily_cap).
4. **Квалификации МИМ** — из `reference.qualification_multipliers` (qualification, multiplier, daily_cap).
5. Объяснение расчёта «потолка дня» (наименьший из домена и ступени/квалификации).

### Разделение шкал

- **Ступени Ученика** (1 Случайный → 5 Проактивный): применяется при `qualification_level = Ученик`.
- **Квалификации МИМ** (Ученик → Общественный деятель): применяется при уровне Работник и выше.

Обе шкалы читаются из БД — hardcode удалён (WP-327 Phase 2).

---

## Связи в коде

| Файл | Что |
|------|-----|
| `handlers/points.py` | `cmd_points()`, `cmd_rules()`, `_progress_bar()`, `_format_event_compact()` |
| `db/queries/rewards.py` | `get_points_balance()`, `get_earned_total()`, `get_today_total()`, `get_user_daily_cap()`, `get_student_stage_multipliers()`, `get_qualification_multipliers_list()` |
| `core/tier_config.py` | `/points` в TIER_MENU_COMMANDS для T2-T4 |
| `bot.py` | `/points` в global BotCommand list |

## Источник истины

`rewards.point_balances` (writer: multi-domain-projection-worker, DP.ROLE.034). Бот **не пересчитывает** баллы — только читает. Формула: WP-121 Ф2 v2, `compute_effective_amount()`.
