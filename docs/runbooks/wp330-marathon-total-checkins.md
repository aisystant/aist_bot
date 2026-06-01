# Runbook: WP-330 P1 — Переход total_checkins на derived (marathon_state)

## Контекст

После peer-session 2026-06-01 колонка `learning.marathon_progress.total_checkins` больше не используется как источник истины. Все читатели (`cmd_marathon_progress`, nudge, mentor alerts, weekly digest) читают `COUNT(DISTINCT day) FROM learning.marathon_state`.

Коммиты: `d799a12` (P0) → `ca52eb4` (P1) → `b64ff16` (fixup) → `bead7d4` (dry-run) → `d2e087e` (write-never).

## Деплой-чеклист

### 1. Pre-deploy (обязательно)

```bash
# 1.1 Установить LEARNING_URL = pilot/prod Neon endpoint (см. Railway env)
# Локальный .env даст misleading PASS — нужны реальные данные активных пользователей.
export LEARNING_URL="postgresql://...pooler.neon.tech/learning?sslmode=require"
python -m scripts.wp330_dry_run_total_checkins
```

**Ожидаемый результат:** exit 1 (расхождения есть у активных пользователей — это нормально, колонка не обновлялась после P0). Exit 0 возможен только если все активные пользователи на день 1 или нет чек-инов.

**PASS-критерий:** скрипт завершился без Python exception и без asyncpg error. Exit code 0/1 — оба приемлемы.

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

Миграция 021 не имеет `migrate_if_needed` и не запускается автоматически при старте бота. Запуск только вручную:

```bash
export LEARNING_URL="postgresql://...pooler.neon.tech/learning?sslmode=require"
python -m db.migrations.021_marathon_stats_derived_checkins
```

**Время блокировки:** ~100–500 мс ACCESS EXCLUSIVE lock на `learning.marathon_stats` (DROP VIEW + CREATE VIEW внутри одной транзакции).

**Сайд-эффект:** параллельные SELECT из Metabase / dt-collect получат гарантированную ошибку `relation "marathon_stats" does not exist` либо заблокируются до CREATE. Это ожидаемо — scheduled refresh Metabase покажет один failed data point.

**Порядок критичен:** миграция 021 ОБЯЗАТЕЛЬНО ДО шага 3 (рестарт бота). Иначе старый код будет читать новое view (безопасно), но runbook этого ожидает.

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

**Реалистичный сценарий — redeploy предыдущего тега:**

1. Railway redeploy на коммит `a5e5662` (HEAD до WP-330 P1 серии). Бот вернётся к старому коду — будет писать в колонку при `callback_marathon_checkin`.
2. Миграция 021 view-only — view `marathon_stats` безопасен и для старого кода (старый код не читает view, только Metabase). Откат миграции не нужен.
3. Колонка `total_checkins` после redeploy постепенно обновится при новых чек-инах. У активных пользователей значения будут расходиться с derived до завершения текущих марафонов.

**Полный revert серии не рекомендуется** — 5 коммитов, переплетение с другими WP. Redeploy надёжнее.

## Known issues

- **«Прыжок» digest/alert:** после деплоя `total_checkins` для активных пользователей может резко измениться (если не выполнен REMEDIATION_SQL). Это корректное поведение — фикс бага.
- **Backfill-расхождение:** старые значения `total_checkins` в колонке могут не совпадать с `COUNT(marathon_state)` из-за предыдущих багов инкремента.
- **Metabase failed refresh:** дашборды на `learning.marathon_stats` покажут одну failed query во время миграции 021. Retry восстановит данные — потери нет.
- **Колонка `marathon_progress.total_checkins` остаётся в схеме:** не читается, не пишется. Удаление колонки отдельным cleanup-РП (не блокер деплоя).

## Артефакты

- Peer-session: `sessions/2026-06/2026-06-01-22-wp330-total-checkins-verify/report.md`
- Dry-run скрипт: `scripts/wp330_dry_run_total_checkins.py`
- Миграция: `db/migrations/021_marathon_stats_derived_checkins.py`
