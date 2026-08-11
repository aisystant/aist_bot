# P-14 Guide Render Trigger

> При достижении «Первокурсника» (X2+X3 оба закрыты) бот ставит запись в общую очередь `learning.guide_render_queue` — асинхронный рендер первого персонального руководства подхватывает отдельный сервис (`render-pilot-guides.py`, DS-autonomous-agents), не бот.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Тип | Процесс (trigger/writer, insert-only) |
| Источник | WP-406 Ф-К, контракт — пир-сессия Claude+Codex 11.08.2026 |
| Файл | `db/queries/guide_render.py` |
| Таблица | `learning.guide_render_queue` (Neon, shared — владелец схемы `activity-hub`) |
| Потребитель | `render-pilot-guides.py --queue-only` (DS-autonomous-agents), systemd-timer каждые ~10 мин — **не синхронно** |
| Вызывающий код | `handlers/onboarding.py:on_x3_confirm`, `core/onboarder/x2.py:_finish_x2` |

---

## 1. Архитектурная граница

Бот — только writer в очередь, не рендерит и не читает результат. Таблица `learning.guide_render_queue` — **общая** для 3+ независимых сервисов (`stage_transition_listener.py`, gateway-mcp `create_repository`, ручной force-render, и теперь этот триггер) — каждый пишет свой `trigger_type`, читает один и тот же consumer.

```
Бот (X2+X3 закрыты) ──► INSERT trigger_type='onboarding_x3' ──► learning.guide_render_queue
                                                                        │
                                                          (poll ~10 мин)▼
                                                    render-pilot-guides.py --queue-only
                                                                        │
                                                                        ▼
                                                       GitHub personal-guide репо пилота
```

Результат рендера бот не получает и не проверяет — доставка отдельным каналом (GitHub, existing flow).

## 2. Точка вызова и идемпотентность

`trigger_first_guide(chat_id, source, trigger_event_id)` вызывается ТОЛЬКО внутри веток, уже защищённых атомарным `mark_x2_done()`/`mark_x3_done()` (`SELECT ... FOR UPDATE` + `newly_marked`) — гарантия «максимум один вызов на пользователя» наследуется от существующего guard'а онбординга, отдельного идемпотентного ключа в очереди не заводится.

Fail-open по всей функции: T0-пользователь (`account_id` ещё не привязан) или сбой INSERT — пропуск с ERROR-логом, онбординг не ломается (закрытие X2/X3 уже зафиксировано раньше вызова).

## 3. Схема

10 колонок, см. `activity-hub/activity_hub/migrations/012_guide_render_queue.sql`. `trigger_type` — CHECK-ограничение, расширено под `'onboarding_x3'` миграцией `017_guide_render_queue_onboarding_x3.sql` (activity-hub, применена к прод `learning` DB 11.08.2026). `trigger_payload` — `{"trigger_event_id": ..., "source": "onboarding_completed"}`.

## 4. Связанные артефакты

- Контракт вызова: `DS-my-strategy/sessions/2026-08/11/2026-08-11-21-wp406-tk-trigger-portnoy/report.md`.
- WP: `DS-my-strategy/inbox/WP-406/WP-406.md` (Ф-К), `WP-521` (будущий получатель — оркестратор конвейера, Ф7, ещё не начат).
- Миграция схемы: `activity-hub/activity_hub/migrations/017_guide_render_queue_onboarding_x3.sql`.
- Сценарий: `docs/scenarios/02-вспомогательные/scenario-02-11-onboarder-x2.md` §4.
