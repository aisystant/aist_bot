# 02.17 Сценарий выбора траектории (Х3)

> **WP-406 Ф5.** Онбордер (DP.ROLE.067) закрывает Х3 — выбор первого курса под ступень.
> Х2 (понимание сообщества) описан в `scenario-02-11-onboarder-x2.md`.
> Архитектура Х3: `core/onboarder/x3.py`.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Триггер | Кнопка «➡️ Выбрать курс» в конце Х2, или `run_x3()` напрямую при повторном входе |
| Два пути | **Fast path**: есть cp-срез → оффер курса сразу. **Bridge path**: нет среза → /diagnose → мост |
| Завершение Х3 | `mark_x3_done` — только при явном «✅ Да, начинаю» (callback `x3_confirm:<stream>`) |
| TTL моста | 1 час (`_RETURN_TO_TTL_SECONDS = 3600`). Истёк → мост сбрасывается без показа оффера |

---

## 1. Fast path — рекомендованный поток уже известен

`get_latest_cp_assessment(account_id)` возвращает `recommended_stream` (значения: `"РР"` для ступени 5, `"S1"`.."S4"` для ступеней 1-4) и опционально `bottleneck_slot`.

Показывается текст вида:
```
🎯 Тебе подходит <поток><узкое место>.
Хочешь начать?
```

Кнопки:
- **«✅ Да, начинаю»** (`x3_confirm:<stream>`) — фиксирует Х3 и закрывает Онбордера.
- **«🔍 Уточнить через диагностику»** (`start_diagnose_for_x3`) — сохраняет `return_to = "x3_offer"` и отправляет в /diagnose. Кнопка показывается только при fast path, не при bridge path (пользователь только что прошёл Диагноста — повторять незачем).

---

## 2. Bridge path — нет cp-среза

`run_x3()` не нашёл cp_assessment:
1. Сохраняет `return_to = "x3_offer"` с отметкой времени в `onboarding_context`.
2. Отправляет:
   ```
   Чтобы подобрать подходящий курс, нужно задать тебе 3–5 вопросов.
   Запусти диагностику: /diagnose — и я сразу предложу подходящий курс.
   ```
3. Пользователь проходит `/diagnose` → `handlers/diagnose.py._finish_diagnose` вызывает `check_x3_return_to_bridge(bot, chat_id, profile)`.
4. Если `return_to == "x3_offer"` и TTL не истёк → `_show_x3_offer(from_bridge=True)` → кнопка «✅ Да, начинаю» без «Уточнить».
5. Мост сбрасывается (`return_to = None`) независимо от TTL.

---

## 3. Подтверждение — mark_x3_done

`x3_confirm:<stream>` callback (`handlers/onboarding.py:on_x3_confirm`):
- Вызывает `storage.mark_x3_done(chat_id)`.
- Закрывает онбординг-поток: Первокурсник подтверждён (Х2 + Х3).
- Следующий `/start` больше не показывает оффер «Освоиться» (`should_offer` вернёт `False`).

---

## 4. Связанные артефакты

| Артефакт | Путь |
|----------|------|
| Обещание | `DP.SC.170` (Онбордер) |
| Роль | `DP.ROLE.067` |
| Код | `core/onboarder/x3.py` |
| Хендлеры | `handlers/onboarding.py` (`on_x3_confirm`, `on_start_diagnose_for_x3`), `handlers/diagnose.py` (`_finish_diagnose`) |
| Диагностика | `handlers/diagnose.py` + `scenario-02-10-test.md` |
| БД | `db/queries/cp_assessment.py:get_latest_cp_assessment` |
| Статус Х3 | `core/onboarder/storage.py:mark_x3_done` |

---

## 5. Инварианты

- `mark_x3_done` — idempotent, idempotent при повторном нажатии.
- `check_x3_return_to_bridge` — не создаёт side-effects если `return_to != "x3_offer"`.
- `_show_x3_offer` — чистая функция, не пишет в хранилище, импортируется без циклического импорта (core ≠ handlers).
- Конец Х3 без подтверждения (`from_bridge=True`, пользователь закрыл бота): следующий вход снова предложит `/diagnose` через bridge path.
