# Сценарий 03-29: /simulator — Симулятор развития

**Вид:** C (Микро) — один экран, inline-кнопки  
**Команда:** `/simulator`  
**Handler:** `handlers/simulator.py`  
**Сервис:** DP.SC.133, DP.ROLE.043 (WP-319 Ф6)

## Описание

Показывает, как изменятся bh-характеристики пилота при разных режимах занятий.  
Использует движок `activity-hub` (S1 — траектория ступеней, горизонт 12 недель).

## Точки входа

| Действие | Callback | Поведение |
|----------|----------|-----------|
| `/simulator` | — | Экран выбора с 7 кнопками |
| Ступень 1-5 | `sim_preset_N` | Типовой профиль N → S1 → pilot_text |
| Мой профиль | `sim_profile` | Реальные bh из Neon → S1 → pilot_text |
| Что если... | `sim_whatif` | FSM → захват текста → LLM-парсинг → S1 |

## Состояния FSM

- `SimulatorStates.waiting_whatif` — ожидание текстового сценария после нажатия «Что если...»

## Источники данных

- **Preset:** `make_preset_profile(stage)` из `activity_hub.engines.simulator.data`
- **Real:** `load_profile(account_id, conn)` — `learning.stage_transitions.evidence`
- **What-if:** `parse_scenario_text(text)` — Claude Haiku tool_use → bh_overrides

## Ограничения

- Если activity-hub недоступен → graceful fallback + ссылка на веб-версию
- Если профиль пользователя не найден → preset ступени 1
- S1 (траектория ступеней) — единственный сценарий в TG; S2/S3 — только в веб-версии
