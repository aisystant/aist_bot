---
family: B
type: scenario
commands: [consent]
tier_access: T1+
status: active
wp: WP-188 (Ф17 + Ф17.10)
---

# 02.13 `/consent` — согласие на трекинг развития (end-to-end)

> Управление opt-in для расчёта ступени мастерства (Случайный → Проактивный).
> Без opt_in=TRUE worker `stage_evaluator` (WP-253 Блок 2) пропускает пользователя при ежедневном пересчёте 04:35 МСК.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/consent`, `/consent opt-in`, `/consent opt-out`, `/consent revoke` |
| Вид | Вспомогательная (B) — inline callbacks через `edit_text`, без FSM |
| Файлы | [`handlers/consent.py`](../../../handlers/consent.py), [`db/queries/consent.py`](../../../db/queries/consent.py) |
| БД | Neon `learning.tracking_consent` (миграция 109+113); writer-pool через роль `consent_writer` (`CONSENT_URL` env) |
| Privacy | B8.0 v1.0, https://system-school.ru/iwe/privacy (WP-212) |
| Доступ | T1+ (требуется привязка LMS-аккаунта `aisystant_suser_id`, иначе нет `account_id`) |

---

## 1. End-to-end flow (для wave-rollout)

```
1. TG-рассылка пилоту → пользователь открывает бот
2. /start → онбординг → /link (если ещё нет) → resolve account_id (Ory UUID из persona.ory_identity)
3. /consent (или кнопка «📊 Согласие на трекинг» в /start) → status экран
4. /consent opt-in (или кнопка «Дать согласие») → privacy-экран + кнопка [✅ Принять]
5. callback consent_accept → UPSERT learning.tracking_consent (account_id, opt_in=TRUE, scope, opted_at=now)
   ↓
6. Подтверждение пользователю: «✅ Согласие зафиксировано. Следующий пересчёт ступени — 04:35 МСК.»
7. Ночью 04:35 МСК — systemd-timer `iwe-stage-evaluator` на tsekh-1:
   ↓
8. stage_evaluator (FORM.089 §5):
     a. SELECT account_id FROM learning.tracking_consent WHERE opt_in=TRUE
     b. Читает M1/M2/M4/W метрики из public.domain_event + learning.w_reflections за окно
     c. Вычисляет stage_raw (рубрики PD.FORM.089 §4)
     d. INSERT INTO learning.stage_transitions (UNIQUE на account_id+to_stage, ON CONFLICT DO NOTHING)
   ↓
9. /me показывает текущий stage; /points показывает балансы.
```

---

## 2. Команды и UI-элементы

| Команда / callback | Эффект | Файл |
|--------------------|--------|------|
| `/consent` | Status экран: opt-in/opt-out/нет, дата, scope, сводка событий 30d | `handlers/consent.py:186` |
| `/consent opt-in` | Privacy-текст + inline [✅ Принять] | там же |
| `/consent opt-out` | UPDATE `opt_in=FALSE` (история сохранена, `opted_at` не трогается) | `db/queries/consent.py:80` (COALESCE) |
| `/consent revoke` | Двухшаговое подтверждение → DELETE строки (GDPR right to erasure) | `handlers/consent.py:317` |
| `consent_accept` | UPSERT с opt_in=TRUE; cache invalidate (race-safe, до+после UPDATE) | `handlers/consent.py:282` |
| `consent_decline` | UPSERT с opt_in=FALSE | `handlers/consent.py:308` |
| `consent_link_now` | Запуск `/link` flow, если LMS не привязан | `handlers/consent.py:344` |
| `consent_from_onboarding` | Точка входа из onboarding (после `/start` / `/link`) | `handlers/consent.py:414` |
| `consent_retry_status` | Повторный показ status (refresh после ETL) | `handlers/consent.py:450` |

---

## 3. Предусловия для opt-in

1. **Привязан LMS-аккаунт** (`persona.ory_identity.aisystant_suser_id`). Иначе `resolve_ory_id_from_chat()` вернёт None → UI ведёт на `/link`.
2. **Бот доступен** (`@aist_me_bot` prod / `@aist_pilot_bot` pilot). Tier T1+.
3. **`CONSENT_URL` env установлен** в Railway сервисе бота → роль `consent_writer` (миграция 113), BYPASSRLS защищён explicit `WHERE account_id` (LESSON: `lessons_bypassrls_gotcha.md`).

---

## 4. Что НЕ делает opt_in

> **Важно (выявлено 12 мая 2026, lesson: `lessons_consent_does_not_imply_activity.md`):** opt_in сам по себе НЕ даёт stage. Нужны practice/learning события (`lesson_completed`, `day_close`, `iwe_session`, `wp_completed`...).

Первый opt_in юзер `433fd7f9` (12 мая 08:25 UTC) имел 36 work events, но 0 practice/learning → stage_evaluator выдал stage=0. Это **не баг**. UX обязан это объяснять (`_activity_summary` в `_format_status`).

Реалистичная цепочка для нового пилота:
1. День 0: opt-in → stage=0
2. День 1–7: использует Day Open/Close, /learn, /train, фиксирует РП → 10+ practice/learning событий
3. День 7–14: первый stage_raw≥1 (Практикующий)

---

## 5. Массовая рассылка пилотам (wave-rollout)

Когда пилот рассылает первой когорте через `aist_me_bot`:

1. **Pre-flight (~5 мин):**
   - SELECT COUNT из `public.domain_event` за 7d сгруппированный по `account_id` — кто из 50 уже активен.
   - Подсчитать, у скольких есть `persona.ory_identity` (LMS-link) — без неё `/consent` упрётся в `consent_link_now`.
2. **Сообщение в TG** (текст готовится в `i18n/schema.yaml` под ключом, например, `consent.broadcast.invitation`):
   - Что трекаем (рубрики ст. 1–5)
   - Как (примерные действия)
   - Ссылка `https://t.me/aist_me_bot?start=consent`
3. **Старт-параметр `consent`** в боте: `cmd_start` распознаёт `?start=consent` → сразу переход на privacy-экран (минуя обычный /start onboarding для тех, кто уже привязан).
4. **Мониторинг:**
   - Прирост `learning.tracking_consent.opt_in=TRUE` (SQL probe)
   - Завтрашний журнал `iwe-stage-evaluator.service` — `processed: N` должен расти
   - `stage_transitions` за окно

> ✅ Пункт 3 (старт-параметр `consent`) — **реализован 12 мая 2026** (WP-188 Ф17). `handlers/onboarding.py:cmd_start` детектирует `args[1] == "consent"` → для онбордированных пользователей вызывает `handlers/consent.py:show_consent_optin()` напрямую (privacy-экран + кнопка [✅ Принять]). Новые пользователи проходят онбординг и видят кнопку «📊 Согласие на трекинг» после auto-link.

---

## 6. Источник истины и связанные документы

| Что | Где |
|-----|-----|
| Privacy текст | B8.0 v1.0 (`DS-ecosystem-development/.../legal/privacy.md`), URL https://system-school.ru/iwe/privacy |
| Алгоритм stage_evaluator | PD.FORM.089 §5 + WP-253 Блок 2 design (`DS-my-strategy/inbox/WP-253-block2-stage-evaluator-design.md`) |
| Worker | `DS-IT-systems/activity-hub/activity_hub/workers/stage_evaluator.py` + systemd `iwe-stage-evaluator.timer` (tsekh-1, 04:35 МСК) |
| Service Clause | DP.SC.020 (event-ingest) + DP.SC.012 (stage-evaluator), `PACK-digital-platform/.../08-service-clauses/` |
| Role | DP.ROLE.032 event-ingester, DP.ROLE.028 stage-evaluator |

---

## 7. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/consent.py` | `/consent` command + 7 callbacks |
| `db/queries/consent.py` | `get_consent`, `set_consent`, `revoke_consent`, `count_practice_events_30d` |
| `db/connection.py` | `_consent_pool` (writer-pool через `CONSENT_URL`); fallback на `LEARNING_URL` |
| `handlers/onboarding.py` | Кнопка «📊 Согласие на трекинг» вшита в onboarding-final |
| `handlers/link.py` | Follow-up consent-кнопка после успешной привязки LMS |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-05-23 | WP-349: После `consent_accept` нудж теперь ведёт на `/setup` (не `/diagnose`). |
| 2026-05-12 | WP-188 Ф17 — реализация `/consent`, writer-pool, GDPR fixes, activity-summary, /link follow-up |
| 2026-05-12 | WP-253 Блок 2 — пароли ролей (stage_evaluator, consent_writer, w_reflection_writer) rotated; `CONSENT_URL` Railway обновлён; smoke-test PASS |
| 2026-05-12 | Создание этого документа (end-to-end процесс) |
