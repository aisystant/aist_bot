# Runbook: WP-330 P1 — Переход total_checkins на derived (marathon_state)

## Контекст

После peer-session 2026-06-01 колонка `learning.marathon_progress.total_checkins` больше не используется как источник истины. Все читатели (`cmd_marathon_progress`, nudge, mentor alerts, weekly digest) читают `COUNT(DISTINCT day) FROM learning.marathon_state`.

Коммиты: `d799a12` (P0) → `ca52eb4` (P1) → `b64ff16` (fixup) → `bead7d4` (dry-run) → `d2e087e` (write-never).

## Деплой-чеклист

### 1. Pre-deploy (обязательно)

```bash
# 1.1 Проверить расхождения колонки vs derived
python -m scripts.wp330_dry_run_total_checkins
```

**Ожидаемый результат:** exit 1 (расхождения есть у активных пользователей — это нормально, колонка не обновлялась после P0).

**Действие:**
- Если нужен плавный переход без «прыжка» — выполнить `REMEDIATION_SQL` из вывода скрипта:
  ```sql
  UPDATE learning.marathon_progress mp
  SET total_checkins = (
      SELECT COUNT(DISTINCT day)
      FROM learning.marathon_state ms
      WHERE ms.user_id = mp.user_id
  )
  WHERE mp.status = 'active';
  ```
- Если «прыжок» принят (колонка не читается после P1) — деплой без sync.

### 2. Миграция БД

```bash
python -m db.migrations.021_marathon_stats_derived_checkins
```

**Время блокировки:** ~100–500 мс (DROP VIEW + CREATE VIEW).

### 3. Деплой бота

- Перезапуск бота применит новый код.
- `start_marathon_flow` больше не пишет в `total_checkins`.
- `callback_marathon_checkin` больше не инкрементирует `total_checkins`.

### 4. Post-deploy verify

```bash
# 4.1 Проверить, что dry-run даёт exit 0 для новых пользователей
python -m scripts.wp330_dry_run_total_checkins

# 4.2 Проверить логи на отсутствие ошибок в 10:00 nudge
# Ожидаемо: 0 ложных nudge "три дня без чек-ина" для пользователей с актуальными чек-инами
```

## Rollback

Если нужен откат:
1. Revert коммитов `d2e087e` → `bead7d4` → `b64ff16` → `ca52eb4` → `d799a12`.
2. Восстановить `total_checkins=0` в `start_marathon_flow` и инкремент в `callback_marathon_checkin`.
3. Вернуть старый view `marathon_stats` из миграции 020.

## Known issues

- **«Прыжок» digest/alert:** после деплоя `total_checkins` для активных пользователей может резко измениться (если не выполнен REMEDIATION_SQL). Это корректное поведение — фикс бага.
- **Backfill-расхождение:** старые значения `total_checkins` в колонке могут не совпадать с `COUNT(marathon_state)` из-за предыдущих багов инкремента.

## Артефакты

- Peer-session: `sessions/2026-06/2026-06-01-22-wp330-total-checkins-verify/report.md`
- Dry-run скрипт: `scripts/wp330_dry_run_total_checkins.py`
- Миграция: `db/migrations/021_marathon_stats_derived_checkins.py`
