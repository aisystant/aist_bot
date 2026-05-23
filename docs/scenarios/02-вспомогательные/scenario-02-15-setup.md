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

### T1 — Старт

```
🎯 Твой уровень на платформе — Т1 «Старт», тебе доступны 14-дневный марафон и личная диагностика.

14 дней × 20 мин/день, можно ставить паузу

• 📚 Марафон — 14 уроков про системное мышление
• 🔍 Диагностика ступени /diagnose

[📚 Начать Марафон]    callback: setup_action:marathon  → start_marathon_flow (DP.SC.157)
[💳 Подписка → T2]    url: _SUBSCRIPTION_URL
[💡 Что ещё?]          callback: setup:what_else
```

### T2 — Изучение

```
🌱 Твой уровень на платформе — Т2 «Изучение», тебе доступны все руководства и знания платформы.

• 📖 Лента /feed
• 🧭 Навигатор /navigator
• 🔍 Диагностика /diagnose
• 📊 Баллы /points и статистика /me

[📖 Открыть Ленту /feed]         callback: setup_action:feed
[🔌 Установить расширение → T3]   callback: setup_step:browser
[💡 Что ещё?]                     callback: setup:what_else
```

### T3 — Персонализация

```
📚 Твой уровень на платформе — Т3 «Персонализация», тебе доступна полная персональная настройка, включая статистику и гида.

• 🤖 Браузерный ассистент: /connect
• 🧭 Личный гид /guide
• 📊 Баллы /points и статистика /me

[🧭 Открыть личный гид /guide]   callback: setup_action:guide
[🐙 Подключить GitHub → T4]       callback: setup_step:github
[💡 Что ещё?]                     callback: setup:what_else
```

### T4 — Созидание

```
🚀 Твой уровень на платформе — Т4 «Созидание», тебе доступны все инструменты платформы.

• 💻 Рабочее окружение (VS Code): /connect
• 🐙 Личная база знаний в GitHub
• 📋 Рабочий план /plan
• 🏛 Клуб /club
• 📊 Баллы /points и статистика /me

[🚀 Открыть план /plan]    callback: setup_action:plan
[📊 Пройти аттестацию]     callback: setup_action:assessment
[💡 Что ещё?]               callback: setup:what_else
```

---

## 3. Callbacks

| Callback | Действие |
|----------|---------|
| `setup_action:marathon` | `start_marathon_flow(user_id, msg)` — запуск марафона напрямую (DP.SC.157) |
| `setup_action:feed` | Ссылка на Ленту → `/feed` |
| `setup_action:guide` | Ссылка на персональный гид |
| `setup_action:plan` | Ссылка на план |
| `setup_action:assessment` | Ссылка на аттестацию |
| `setup_step:browser` | Инструкция по установке браузерного расширения + `[✅ Установил]` |
| `setup_step:github` | Инструкция по подключению GitHub + `[✅ Подключил]` |
| `setup_tool_done:browser` | Подтверждение браузера → редирект на `/connect` |
| `setup_tool_done:github` | Подтверждение GitHub → редирект на `/github` |
| `setup:what_else` | Показать «Что ещё?» экран с tier-aware командами (DP.SC.156) |
| `setup:tier_back` | Удалить «Что ещё?» и вернуть tier-экран |

---

## 4. Возвращение (re-entry)

Если `last_active_date < today` → к тексту добавляется «С возвращением!» в начало.

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-05-23 | WP-349: создание сценария. handlers/setup.py переписан с 4 тир-экранами. |
| 2026-05-23 | WP-349 Ф11-Ф14: канонические tier-сообщения (DP.SC.158), марафон 8→4 действия (DP.SC.157), кнопка «Что ещё?» (DP.SC.156). Post-consent → tier screen напрямую. |
