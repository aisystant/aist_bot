# Release Drift Manifest: pilot → new-architecture, кандидат от 01.11.2026

> Журнал классификации дельт по P-18 §5 (`docs/processes/process-18-release-contract.md`) и план миграций к выпуску (шаги 2–3 плана сверки РП-562).
> Ревизия 4 · 01.11.2026 · автор классификации: Claude Code (роль редактора по §5). Ревизия 2: результаты проверок 4.1 и 4.2. Ревизия 3: снят D9 (раздел 3). Ревизия 4: контур выпусков (D1) не выпускается до включения контроля, выпуск собирается переносом без него (раздел 6). История — раздел 8. Подтверждающий и решение пилота не заполнены — их ставят критик и пилот.
> Подготовлено без подключения к живым базам: всё, что зависит от фактического состояния баз, вынесено в проверки «только чтение» для владельца (раздел 4).

## 1. Шапка выпуска

| Поле | Значение |
|---|---|
| Release ID | не присвоен — назначает release owner |
| Артефакт | `git_commit_sha_surrogate` (§0): кандидат по образу не собирался |
| Pilot SHA | ревизии 1–3: `a9a83d15`; ревизия 4: `c188fdee0707c447122804a5eff0b76e39111cc1` (код бота между ними не менялся, только этот документ) |
| Production SHA | `66e03747cf26cfc5bb37000127f3c3728df3a194` |
| Объём | 101 файл отличается (вместе с этим документом). Выпуск переносит 62 из них; 33 файла контура, 2 задания CI и 4 скрипта/теста Railway остаются только в pilot (раздел 6) |
| CI на pilot SHA | Smoke Tests (L1+L2), Release Control Contract, Security (bandit + pip-audit) — зелёные |
| Решение пилота | — |

## 2. Шум переноса (`transfer-noise`)

| # | Что | Доказательство | Статус |
|---|---|---|---|
| N1 | 5 коммитов только в production: 93e9553f, 9dbfd19a, 0dc13827, 66e03747 (Аналитик чата, РП-612 Ф5), 0fc128e3 (база стиля, РП-388 Ф24) | `git cherry origin/pilot origin/new-architecture` — все пять с `-` (совпадает patch-id); затронутые файлы в pilot идентичны | transfer-noise |
| N2 | 27 записей детектора `new_architecture_only` (04c8a690, 2a5c7c2c, f9537f6c и др.) | `git merge-base --is-ancestor <sha> origin/pilot` — все в истории pilot (вошли слиянием f060ab8a, РП-388 Ф28). Детектор держит их открытыми намеренно (P-18 §9: пропажа SHA не закрывает долг, закрывает только равенство деревьев) — см. D9 | transfer-noise |
| N3 | Около 3 тыс. записей детектора `pilot_only` за январь–октябрь | История веток различается с января (прод собирался переносами); содержательное расхождение — только итоговое дерево (раздел 3) | transfer-noise; содержание учтено в D1–D8 |

## 3. Реальные дельты к выпуску (`real-delta`, направление pilot → production)

Все дельты ниже — новая работа pilot, ещё не выпущенная. Production-only долга (хотфиксов без переноса) нет.

| # | Группа | Файлы | Риск (§5) | Предусловие выпуска | Статус |
|---|---|---|---|---|---|
| D1 | Контур выпусков РП-562: скрипты и тесты контроля выпуска, P-18, задания CI, цели сборки образа | `scripts/release*`, `scripts/*release*`, `scripts/check_*`, `scripts/railway_*`, `scripts/run_release_smoke.py`, `release_manifest.py`, `.github/release-*`, `.github/workflows/{pilot-prod-sync,release-control-dry-run,release-candidate-build,smoke-tests,security}.yml`, `Dockerfile`, `.dockerignore`, `docs/processes/process-18-release-contract.md`, `tests/test_release*`, `tests/test_branch_auto_deploy.py`, `tests/test_railway_*`, `tests/smoke/test_behavior_scenarios.py` | инфраструктура CI/сборки | см. примечание D1 | real-delta, **не выпускается до включения контроля** (ревизия 4): pilot-only по замыслу РП-562 |
| D2 | Оплаты Мастерской (WP-572): прямая покупка, проверка схемы при старте, оплата проходит мимо лимита сообщений и режима обслуживания | `handlers/workshop.py`, `handlers/showcase.py`, `db/connection.py`, `db/models.py`, `db/migrations/046_workshop_payments_product.py` (`IF NOT EXISTS`), `core/middleware.py`, `docs/processes/process-12-payment-events.md`, сценарий 03-35, `tests/smoke/test_workshop_*`, `tests/smoke/test_showcase_payment_routing.py`, `tests/smoke/test_middleware.py` | **billing** — нужно подтверждение пилота | колонка `workshop_payments.product` в основной базе прода (проверка 4.1) — **есть, 01.11** | real-delta, к выпуску |
| D3 | Баллы РП-567: миграции 050 и 051 и их тесты | `db/migrations/050_*`, `db/migrations/051_*`, `tests/smoke/test_wp567_*` | **billing** — нужно подтверждение пилота | нет: бот их не выполняет (раздел 5) | real-delta, к выпуску |
| D4 | Привязка аккаунта Ory: решение о привязке до записи, понятный ответ 409, источник регистрации FPF | `db/queries/identity.py`, `oauth_server.py`, `handlers/onboarding.py`, `core/onboarder/__init__.py`, сценарии 02-01 и 02-11, `tests/test_ory_account_already_linked.py`, `tests/smoke/test_wp406_f16_entry_source.py` | **auth** — нужно подтверждение пилота | — | real-delta, к выпуску |
| D5 | Зеркало профиля в persona.bot_profile (WP-253 Ф12.6-A), за флагом, по умолчанию выключено | `db/queries/bot_profile.py`, `db/queries/users.py`, `db/queries/dev_stats.py`, `config/settings.py`, `config/__init__.py`, часть `oauth_server.py`, `tests/test_bot_profile_mirror.py`, `tests/smoke/test_wp330_onboarding_correctness.py` | **GDPR** (копия полей профиля в другую базу) — нужно подтверждение пилота | `BOT_PROFILE_DUAL_WRITE_ENABLED` в проде не задан или `false` (проверка 4.2) — **не задан, 01.11** | real-delta, к выпуску |
| D6 | Уведомления (WP-117): арбитр противоречивых уведомлений, «низкая активность» по живым событиям, полярность milestone C3 | `core/claims.py`, `core/activity_sources.py`, `core/engagement_analyzer.py`, `core/nudge_producer.py`, `core/scheduler.py`, `db/queries/{conversion,nudges,notifications}.py`, `tests/test_cross_pipeline_claims.py`, `tests/test_activity_source_registry.py`, `tests/test_engagement_analyzer.py` | поведение для пользователей | — | real-delta, к выпуску |
| D7 | Прочее: фиксации только при наличии источника записи (App или OAuth в льготный период), событие `lesson_closed` (WP-522), поля отчёта сессий (F121), подсказка `/mentor_card`, владелец резервной ссылки self-knowledge, тест резервного пути подписки | `core/evaluator.py`, `core/lesson/*`, `handlers/external_session.py`, `handlers/mentorship.py`, `core/self_knowledge.py`, `docs/data/tables.md`, сценарий 03-38, `tests/test_{fixation_note_app_only,lesson_events,f121_report_fields,has_active_subscription}.py` | низкий | — | real-delta, к выпуску |
| D8 | Тесты оповещения `installation_orphaned` (РП-559) | `tests/smoke/test_internal_orphan_notify.py` | низкий (только тесты) | — | **устранён по поведению**: код обработчика в обеих ветках одинаковый, тесты и раздел P-06 вернулись в pilot (aisystant/aist_bot#351). Файл тестов отличается от прода только способом вызова обработчика и уйдёт с выпуском |

**Примечание D1, ревизия 4.** Защита `release-control-import-guard` (`scripts/release_control_import_guard.py`) не пускает в `new-architecture` 33 пути из `.github/release-control-contour.json`, пока для этой ветки не включён контроль выпусков (`.github/release-control-enforcement.json` отсутствует, `cutover.enabled=false`). Ревизии 1–3 ошибочно относили весь D1 к выпуску; это всплыло на проверке запроса aisystant/aist_bot#354. Кроме 33 путей, в pilot остаются: `.github/workflows/smoke-tests.yml` и `.github/workflows/release-candidate-build.yml` (в проде остаются их текущие версии, новые вызывают скрипты контура), `scripts/railway_release_provider.py` (импортирует `scripts/release_control.py`; в список контура не внесён), `scripts/railway_graphql_mutation_candidate.py` и тесты обоих (та же фаза Ф3.3). Из D1 в выпуск идут только: `Dockerfile`, `.dockerignore`, `.github/workflows/security.yml`, `.github/release-metadata.json`, `scripts/release-drift-detector.py`, `docs/processes/process-18-release-contract.md` — все вне списка контура. Ниже — что из этого меняется в проде.

**Что меняется в проде (ревизии 1–3; пункты про `release-candidate-build.yml`, `smoke-tests.yml` и `pilot-prod-sync.yml` ревизией 4 сняты):**
- Сборка Railway не меняется: в `Dockerfile` без явной цели собирается последняя стадия `development`, а это та же стадия `application`, что и сейчас. `.dockerignore` пропускает ровно те каталоги, которые `Dockerfile` копирует.
- `release-candidate-build.yml` перестаёт публиковать образ `ghcr.io/aisystant/aist_bot-prod` при push в `new-architecture`; публикуется только образ pilot. В репозитории его упоминают только тесты (`tests/test_railway_graphql_mutation_candidate.py`), а Railway деплоит прод из Git (`railway.json` без образа), так что деплой это не затрагивает.
- `smoke-tests.yml` на `new-architecture` начнёт запускать проверки через `scripts/run_release_smoke.py`, без `paths-ignore` (проверки идут и на правки документации).
- `pilot-prod-sync.yml` появится в основной ветке, но выключен, пока не задана `RELEASE_DRIFT_ENABLED` (шаг 5 плана).

**D9 — снято в ревизии 3: правка детектора не нужна и была бы вредной.** Записи N2 детектор держит открытыми не по ошибке, а по контракту (P-18 §9): исчезновение SHA или попадание коммита в историю другой ветки не доказывает, что правка там осталась. Пример — РП-559: правку слили в pilot, а через пять минут откатили; закрытие «по предку» записало бы этот долг как устранённый. Закрывает записи только равенство итоговых деревьев. Проверено моделированием: локальное слияние pilot в production (итоговое дерево `47747c70` = дерево pilot) и запуск детектора на копии начального журнала дают `status=clean`, 0 открытых записей, 42 закрыты по равенству деревьев (15 `pilot_only`, 27 `new_architecture_only`). После выпуска журнал очищается сам.

## 4. Проверки «только чтение» перед выпуском (выполняет владелец базы или окружения)

### 4.1. Колонка `product` в основной базе прода — блокирует выпуск · **выполнено 01.11: колонка есть, миграция 046 не нужна**

Код pilot при старте вызывает `verify_money_tables()`. Если колонки нет, бот падает с `RuntimeError` и в прод не выходит. В проде `SKIP_DB_MIGRATIONS=true`, поэтому сам он колонку не создаст.

```sql
SELECT EXISTS (
  SELECT 1 FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name = 'workshop_payments' AND column_name = 'product'
);
```

- `true` → предусловие выполнено. Скорее всего, так и есть: код прода уже пишет `product` при каждой оплате Мастерской (`db/queries/workshop.py`).
- `false` → до слияния владелец основной базы прода выполняет миграцию 046 (`python -m db.migrations.046_workshop_payments_product`); после этого повторить запрос.

### 4.2. Флаг зеркала профиля в окружении прода · **выполнено 01.11 по скриншотам (часть списка): переменной нет, по умолчанию выключено; окончательно — поиск `BOT_PROFILE` в Railway**

В переменных Railway для прода `BOT_PROFILE_DUAL_WRITE_ENABLED` не задан или `false`. Pilot и prod делят одну базу persona; включение — отдельное решение по WP-253, не часть этого выпуска.

### 4.3. Состояние базы баллов (для РП-567, выпуск бота не блокирует)

```sql
-- 050: есть ли у роли воркера право выполнять функцию начисления
SELECT has_function_privilege(
  'neondb_owner',
  'public.compute_effective_amount_v4(uuid, bigint, text, jsonb, timestamp with time zone)'::regprocedure,
  'EXECUTE');

-- 051 (выборочно; полный список прав — GRANTS и MISSING_PRIVILEGES_SQL в самой миграции):
-- выданы ли узкой роли права и может ли владелец на неё переключиться
SELECT has_table_privilege('projection_writer_rewards', 'public.point_balances', 'SELECT')        AS pb_select,
       has_column_privilege('projection_writer_rewards', 'public.point_balances', 'points', 'UPDATE') AS pb_points_update,
       has_table_privilege('projection_writer_rewards', 'public.typing_daily', 'SELECT')          AS td_select,
       has_table_privilege('projection_writer_rewards', 'public.first_payment_guard', 'SELECT')   AS fpg_select,
       pg_has_role('neondb_owner', 'projection_writer_rewards', 'SET')                            AS owner_can_set;
```

## 5. План по миграциям 050 и 051

**Главное: выпуск бота от этих миграций не зависит.** Обе — ручные скрипты для отдельной базы баллов (`REWARDS_URL`). Бот их не импортирует и при старте не выполняет: в `bot.py` их нет, на них ссылаются только их собственные smoke-тесты. Они меняют права ролей, через которые пишет воркер проекций баллов (multi-domain-projection-worker), а не схему, которую читает бот. Поэтому формула «до выпуска или вместе с ним, но не после» к ним не применяется: слияние pilot в прод только доставляет файлы. Порядок их выполнения задаёт РП-567, а не РП-562.

| | 050 | 051 |
|---|---|---|
| Что делает | Возвращает `neondb_owner` право EXECUTE на `compute_effective_amount_v4`, потерянное после миграции 048 | Выдаёт узкой роли `projection_writer_rewards` права по колонкам, чтобы воркер перешёл на неё с `neondb_owner` |
| Зачем | Инцидент: начисления остановились 10.10.2026, воркер получал `permission denied` | Шаг 2 РП-567 Ф5; само переключение воркера (`DATABASE_URL_REWARDS_PROJECTION`) — отдельно, после 051 |
| Повторный запуск | Безопасен: `migrate_if_needed` выходит, если право уже есть | Безопасен: выполняется целиком при каждом запуске (GRANT при повторе ничего не меняет), проверка после выполнения откатывает все пробные записи; `migrate()` возвращает, каких прав не хватало до запуска |
| Откат | `REVOKE EXECUTE ... FROM neondb_owner` под `rewards_points_engine_owner` (в docstring) | `REVOKE ALL ... FROM projection_writer_rewards` и, если выдавалось, членство (в docstring) |
| Вероятное состояние | Миграция написана 27.10 как починка инцидента — скорее всего, уже применена | В docstring: первый живой запуск 28.10 остановился на SET ROLE, в тот же день миграцию поправили — вероятно, применена после этого |

Применены ли они на самом деле, из репозитория не видно. Это показывают запросы 4.3 или карточка РП-567.

**Порядок:**
1. Владелец базы баллов выполняет запросы 4.3.
2. 050 вернула `false` → начисления до сих пор сломаны. Это инцидент РП-567, срочно и независимо от выпуска: выполнить 050, затем отдельно — пропущенные с 10.10 события (backfill по карточке РП-567).
3. 051 вернула хоть один `false` → выполнить 051 до переключения воркера на узкую роль, не раньше и не в связке с выпуском бота.
4. Автору РП-567 показать пометку `# nosec B608` в миграции 051 (добавлена в aisystant/aist_bot#351) и подтвердить обоснование: все подстановки в SQL — константы файла (`ROLE`, `GRANTS`, `TABLES`), текст `MIGRATION_SQL` не изменился.

## 6. Порядок выпуска (для решения пилота)

1. Подтверждение пилота по дельтам категорий billing (D2, D3), auth (D4), GDPR (D5) — §5 п.5. Подтверждающий-критик по классификации (§5 п.3).
2. ~~Проверки 4.1 и 4.2~~ — выполнены 01.11, оба предусловия выполнены (раздел 8).
3. Живая проверка на пилотном боте затронутых сценариев: прямая покупка Мастерской (D2), привязка аккаунта Ory, включая случай «уже привязан» (D4), онбординг с источником FPF (D4), запись фиксации (D7), `/mentor_card` без reply (D7).
4. ~~Слияние `pilot` → `new-architecture`~~ (ревизия 4). Выпуск собирается переносом: от `new-architecture` берутся 62 файла из pilot, без файлов контура, без двух заданий CI и без скриптов Railway. Перенос, а не слияние: если слить pilot и удалить контур, при будущем включении контроля слияние не вернёт удалённые файлы. Проверено на собранном дереве: защита контура — OK; быстрые проверки командой из `smoke-tests.yml` прода — 629 прошли; bandit — замечаний нет; полный `tests/` без smoke — те же 13 падений, что на текущем проде и на pilot (новых нет). Запрос — черновик из `claude/happy-fermat-upltdd` в `new-architecture`; при слиянии кнопкой GitHub хук `FORCE_PROD` не участвует.
5. После деплоя: в журнале старта прода — `✅ REQUIRED_FOR_MONEY schema present: ...; public.workshop_payments.product`. ~~Затем включить наблюдателя~~ (ревизия 4): `pilot-prod-sync.yml` входит в контур и попадёт в прод только при включении контроля. До тех пор детектор, запущенный вручную, будет показывать разницу деревьев — это контур, по замыслу живущий только в pilot.
6. ~~Отдельным запросом — D9~~ — снято (раздел 3, D9): после выпуска детектор покажет `status=clean` сам.

**Откат выпуска:** откатить коммит выпуска в `new-architecture` новым коммитом (`git revert <release commit>`; если запрос слит слиянием — `git revert -m 1 <merge>`), push с `FORCE_PROD=1`. Миграция 046 (если выполнялась) обратно совместима: колонка допускает NULL, старый код её уже использует.

## 7. Подтверждения

| Роль | Кто | Решение | Время |
|---|---|---|---|
| Автор классификации (редактор) | Claude Code | классификация выше | 01.11.2026 |
| Критик | — | — | — |
| Пилот (billing/auth/GDPR, выпуск) | — | — | — |

## 8. Журнал ревизий и проверок

| Ревизия / проверка | Кто | Результат | Источник | Время |
|---|---|---|---|---|
| Ревизия 1 | Claude Code | классификация D1–D9, план миграций | этот файл | 01.11.2026 |
| Проверка 4.1 | пилот (запрос к основной базе прода) | `workshop_payments.product` есть | сообщение пилота в сессии РП-562 | 01.11.2026 |
| Проверка 4.2 | пилот (переменные Railway прода) | `BOT_PROFILE_DUAL_WRITE_ENABLED` в видимой части списка нет; по алфавиту он стоит до `CHATWOOT_URL`, то есть в видимой части. Не показан только участок от `CHATWOOT_URL` до `VOICE_MAX_DURATION_SEC`. Для полной уверенности — поиск `BOT_PROFILE` в переменных Railway | скриншоты переменных (часть списка), сообщение пилота | 01.11.2026 |
| Ревизия 2 | Claude Code | внесены результаты 4.1 и 4.2 | этот файл | 01.11.2026 |
| Ревизия 3 | Claude Code | D9 снят: правка детектора противоречила бы P-18 §9; моделирование выпуска даёт `status=clean` | этот файл | 01.11.2026 |
| Ревизия 4 | Claude Code | D1 не выпускается до включения контроля; выпуск — перенос 62 файлов без контура; наблюдатель отложен | этот файл, aisystant/aist_bot#354 (проверка `release-control-import-guard`) | 01.11.2026 |
