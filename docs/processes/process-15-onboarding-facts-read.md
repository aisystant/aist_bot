# Процесс 15: Read-контракт фактов онбординга для шлюза (WP-406 Ф33 §4)

**Категория:** Процесс (чтение; данные не создаются и не меняются)
**Код:** `oauth_server.py::onboarding_facts_handler`, `db/queries/onboarding_facts.py`
**Дизайн:** `DS-my-strategy/inbox/WP-406/concept-checklist-data-ownership-2026-08-10.md` §4
(АрхГейт 10.08); реализация — пир-сессия `2026-08-11-25-wp406-onboarding-facts-read`.

## Что делает

`GET /internal/onboarding-facts?user_id=<ory_uuid>` — отдаёт gateway-mcp ровно 4 факта
онбординга, которые сегодня знает только бот: `telegram_linked`, `guide_issued`,
`meeting_held`, `trajectory_confirmed`. Каждый факт: `{status, source, occurred_at}`;
ответ несёт `contract_version: "onboarding-facts-v2"` (v2 с 12.08.2026 — решение пилота: статус `absent` вместо натяжки `pending_evaluation`; v1 нигде не деплоился с потребителями).

## Статусная вокабула

`confirmed` — факт наблюдён · `absent` — авторитетное «факта нет у владельца»
· `unknown` — сбой чтения источника ИЛИ пробел наблюдаемости. Отдельного
`failed` нет намеренно; `dead_letter` рендер-очереди даёт `absent` — принятое
ограничение v2 («не выдан» верно, «скоро будет» из статуса не выводится).

## Источники фактов

| Факт | Источник | confirmed когда |
|------|----------|-----------------|
| telegram_linked | `persona.ory_identity` | есть строка с telegram_id (occurred_at ≈ created_at строки) |
| guide_issued | `learning.guide_render_queue` | есть строка `status='done'` (max(completed_at)) |
| meeting_held | нет стола (С2) | никогда в v1 — всегда unknown |
| trajectory_confirmed | `development.user_state.x3_completed_at` | отметка X3 стоит |

Fail-open по фактам: сбой одного источника → `unknown` только этому факту, HTTP всегда 200.

## Безопасность

- HMAC-SHA256 над канонической строкой `GET\n/internal/onboarding-facts\nuser_id=<uuid>\n<ts>`;
  секрет `GATEWAY_ONBOARDING_READ_SECRET`, ротация через `_PREVIOUS` (dual-key, как у
  workbook-webhook).
- Окно timestamp ±300с; anti-replay кэш подписей в границах процесса (после рестарта окно
  открывается заново — принятый остаточный риск v1).
- Ранние отказы до HMAC и БД: query >256 симв., дубль `user_id`, не-UUID → 400;
  битый timestamp → 403. Бизнес-отсутствие пользователя — всегда 200 (anti-enumeration).
- Секрет не задан → 503 (эндпойнт выключен по построению).

## Env

`GATEWAY_ONBOARDING_READ_SECRET` (обязателен), `GATEWAY_ONBOARDING_READ_SECRET_PREVIOUS`
(только на период ротации). Потребитель настраивается на стороне gateway-mcp — отдельная
фаза РП406, этот процесс описывает только серверную сторону бота.

## Тесты

`tests/smoke/test_wp406_f33_onboarding_facts.py` — 15 тестов: подпись/ротация/replay/
timestamp-векторы (включая юникод-цифры и >4300 цифр), маппинг строк БД в статусы,
изоляция сбоя одного источника.
