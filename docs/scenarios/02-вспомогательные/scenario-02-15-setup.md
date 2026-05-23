---
family: B
type: scenario
commands: [setup]
tier_access: T1+
status: active
wp: WP-349
---

# 02.15 `/setup` — экран оснащения (тир-экраны T1→T4)

> Показывает пользователю текущий тир и 2 действия: функциональное (что делать сейчас) + подключение (как перейти на следующий тир).
> Заменяет прежний dashboard-подход (7 чеклист-шагов) на 4 чистых тир-экрана.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/setup` |
| Вид | Вспомогательная (B) — inline callbacks |
| Файл | [`handlers/setup.py`](../../../handlers/setup.py) |
| Tier | T1+ (требуется привязка аккаунта) |
| Место в онбординговом пути | После `/consent` → `/setup` |

---

## 1. Логика определения тира

`/setup` читает состояние пользователя из 4 источников (asyncio.gather):
- `tier_detector.detect(chat_id)` → `UITier` (T1/T2/T3/T4)
- `cp_assessment` → ступень мастерства
- `onboarding_state` → `first_use_guide_render`, `first_use_connect_full`
- `intern` → `last_active_date` (для «С возвращением!» префикса)

---

## 2. Тир-экраны

### T1 — Старт (подписка не подключена)

```
[С возвращением! / Ваш путь оснащения]

🟢 Тир: Старт

Вы начали путь...

[📚 Начать Марафон]    callback: setup_action:marathon
[💳 Подключить подписку (URL)]
```

### T2 — Изучение (подписка есть, браузер не подключён)

```
🟡 Тир: Изучение

Подписка активна...

[📖 Открыть Ленту]       callback: setup_action:feed
[🔌 Установить расширение]  callback: setup_step:browser
```

### T3 — Персонализация (браузер есть, GitHub не подключён)

```
🔵 Тир: Персонализация

Расширение установлено...

[🧭 Открыть гид]       callback: setup_action:guide
[🐙 Подключить GitHub]  callback: setup_step:github
```

### T4 — Созидание (полное окружение)

```
🟣 Тир: Созидание

Полное окружение подключено...

[🚀 Открыть план]         callback: setup_action:plan
[📊 Пройти аттестацию]    callback: setup_action:assessment
```

---

## 3. Callbacks

| Callback | Действие |
|----------|---------|
| `setup_action:marathon` | Сообщение со ссылкой на Марафон → `/learn` |
| `setup_action:feed` | Ссылка на Ленту → `/feed` |
| `setup_action:guide` | Ссылка на персональный гид |
| `setup_action:plan` | Ссылка на план |
| `setup_action:assessment` | Ссылка на аттестацию |
| `setup_step:browser` | Инструкция по установке браузерного расширения + `[✅ Установил]` |
| `setup_step:github` | Инструкция по подключению GitHub + `[✅ Подключил]` |
| `setup_tool_done:browser` | Подтверждение браузера → редирект на `/connect` |
| `setup_tool_done:github` | Подтверждение GitHub → редирект на `/github` |

---

## 4. Возвращение (re-entry)

Если `last_active_date < today` → к тексту добавляется «С возвращением!» в начало.

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-05-23 | WP-349: создание сценария. handlers/setup.py переписан с 4 тир-экранами. |
