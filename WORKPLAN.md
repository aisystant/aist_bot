---
type: workplan
updated: 2026-02-10
---

# WORKPLAN — AIST Bot (new-architecture)

> Операционный план рабочих продуктов.
> **Стратегия эволюции:** [MAPSTRATEGIC.md](MAPSTRATEGIC.md) (Phase 0 → Phase 3)
> **Текущая фаза:** Phase 0 — Стабилизация + Mode-aware routing

---

## Месячные приоритеты (февраль 2026)

| # | Приоритет | Фаза | Бюджет | Статус |
|---|-----------|------|--------|--------|
| 1 | **Стабилизация для марафона 14 фев** — mode-aware routing, баг-фиксы | Phase 0 | 6h | in-progress |
| 2 | Рефакторинг UI/UX — кнопки и сценарии | Phase 0 | 4h | pending |
| 3 | Диспетчер + глобальные префиксы (`.` заметка → inbox, `?` вопрос → ИИ) | Phase 1 | 4h | pending |
| 4 | Подключение digital-twin-mcp | Phase 1 | 2h | pending |
| 5 | GitHub интеграция (заметки, отчёты) | Phase 1 | 4h | pending |

---

## Недельный план (W07: 9–14 фев)

> **Фокус:** Phase 0 — бот должен работать для марафона.

### РП 1: Баг-фиксы

| Баг | Файл | Бюджет | Статус |
|-----|------|--------|--------|
| Legacy router перехватывал сообщения у SM → практика не показывалась | handlers/__init__.py, commands.py, callbacks.py, fallback.py | 30m | done |
| `digest.py:258` — Markdown parse error в дайджесте (Claude-контент с незакрытыми сущностями) | states/feed/digest.py | 30m | in-progress |
| MCP-серверы (Guides, Knowledge) — circuit breaker OPEN, таймаут 15s | clients/mcp.py, config | 1h | pending |
| `digest.py:559` — hardcoded "Возвращайтесь завтра" в feed progress | states/feed/digest.py | 30m | pending |
| `bot.py:1078` — /learn всегда роутит в Марафон | bot.py | 30m | pending |
| `transitions.yaml:71` — come_back → mode_select (показывает меню) | config/transitions.yaml + states/ | 30m | pending |
| `bot.py:3165` — scheduler только для Марафона | bot.py | 30m | pending |

**Критерии готовности:**
- [x] SM — единственный путь обработки сообщений (legacy router удалён, коммит c7f81c6)
- [ ] Дайджест Ленты отображается без Markdown-ошибок (fallback без форматирования)
- [ ] MCP-серверы отвечают (или graceful degradation при таймауте)
- [ ] /learn в режиме Feed → открывает Ленту (не Марафон)
- [ ] /learn в режиме Marathon → открывает Марафон (как сейчас)
- [ ] "Возвращайтесь завтра" → не показывает меню выбора режима
- [ ] Feed progress показывает актуальный статус (не hardcoded текст)
- [ ] Scheduler отправляет уведомления для обоих режимов

### РП 2: Mode-aware routing

| Задача | Файл | Бюджет | Статус |
|--------|------|--------|--------|
| Функция `get_user_mode_state(user)` | core/helpers.py (новый) | 30m | pending |
| Рефакторинг /learn → mode-aware | bot.py | 30m | pending |
| Рефакторинг callback learn → mode-aware | bot.py | 30m | pending |
| come_back и day_complete → остаёмся в режиме | transitions.yaml + states/ | 30m | pending |

**Критерии готовности:**
- [ ] Единая функция определяет стейт по режиму пользователя
- [ ] Все entry points используют эту функцию
- [ ] Нет прямых ссылок на "workshop.marathon.lesson" в bot.py

### РП 3: Smoke Test

| Сценарий | Бюджет | Статус |
|----------|--------|--------|
| Марафон: Урок → Вопрос → Бонус → Задание → "Завтра" | 30m | pending |
| Лента: Темы → Дайджест → Фиксация → "Завтра" | 30m | pending |
| /learn из Марафона и из Ленты | 15m | pending |
| /progress из обоих режимов | 15m | pending |
| Scheduler: уведомление в обоих режимах | 15m | pending |

**Критерии готовности:**
- [ ] Все сценарии проходят без ошибок в @aist_pilot_me

---

## Недельный план (W08: 14–21 фев) — предварительный

> **Фокус:** Phase 0 завершение + начало Phase 1

| РП | Фаза | Бюджет | Статус |
|----|------|--------|--------|
| Мерж newarchitecture → main (если smoke test ок) | Phase 0 | 1h | pending |
| UI/UX: новые кнопки и сценарии | Phase 0 | 4h | pending |
| Извлечь core/dispatcher.py из bot.py | Phase 1 | 2h | pending |
| Глобальные `.` (заметка) и `?` (консультация) | Phase 1 | 2h | pending |

---

## Бэклог (Phase 1+)

| Задача | Фаза | Приоритет |
|--------|------|-----------|
| Подключение digital-twin-mcp (чтение/запись прогресса) | Phase 1 | high |
| `.` заметка → коммит в GitHub через API | Phase 1 | high |
| `?` консультация → guides-mcp | Phase 1 | high |
| /rp, /report, /twin — новые команды (заглушки) | Phase 1 | medium |
| MCP Registry (yaml) | Phase 2 | medium |
| Агент Стратег — отдельный сервис | Phase 2 | medium |
| Агент Консультант — классификация уровня | Phase 2 | medium |
| GitHub OAuth для личных репо | Phase 2 | low |
| Telegram WebApp (сложный UI) | Phase 3 | low |

---

*Последнее обновление: 2026-02-10*
