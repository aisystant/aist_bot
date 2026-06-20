---
family: C
type: scenario
commands: [/day]
tier_access: T2-T4
status: active
wp: WP-428
related_sc: DP.SC.184
---

# Scenario 03-33: Day Open Digest (`/day`)

**Вид:** C (Микро) — команда без FSM-состояний, один ответ  
**Хендлер:** `handlers/day.py` `on_day`  
**РП:** WP-428 Ф3  
**Service Clause:** DP.SC.184 (draft)

## Триггеры

| Триггер | Пример |
|---------|--------|
| Команда `/day` | `/day` |

## Пользователи и тиры

| Тир | Поведение |
|-----|-----------|
| T1 (нет подписки) | «Открытие дня доступно с подпиской. Оформи: /subscribe» |
| T2 (подписка, IWE не подключён) | Краткий анонс без персональных данных + CTA `/connect` |
| T3+ (IWE подключён) | Полный дайджест: ритм + активные РП + фокус-задача дня |

## Блокираторы (SkipHandler)

- Активная SM ждёт ответа (`_sm_is_expecting_reply`)
- Фиксация дайджеста (`FeedDigestState.is_waiting_fixation`)
- Онбординг не завершён → «Сейчас недоступно. Попробуй позже.»

## Данные / контекст (data-scope guard)

| Слой данных | T1-T2 | T3+ |
|-------------|-------|-----|
| Ритм (баллы сегодня / всего) | нет | из `rewards` DB через `ory_id` |
| Активные РП | не передаются | 🔄-строки из `current/active-wp.md` GitHub-репозитория стратега, ≤800 символов |
| Архивные / closed РП | никогда | никогда |
| Fallback (нет `active-wp.md`) | — | Hermes отвечает без имён РП (промпт `_HERMES_PROMPT_NO_WP`) |

**Инвариант privacy:** WP-номера — не PII, но являются стратегическим контекстом пользователя. Они никогда не передаются на тир T1-T2. T3+: передаются только актуальные (🔄) РП через system-prompt Hermes, не возвращаются пользователю в сыром виде — только в виде синтеза Hermes.

## Поток (T3+)

```
/day
  └── guard: SM/feed ждут? → SkipHandler
  └── get_intern + onboarding_completed? → unavailable если нет
  └── detect_ui_tier → T1/T2/T3+
  └── T1 → subscribe CTA
  └── T2 → connect CTA
  └── T3+:
        ├── resolve_ory_id_from_chat
        ├── get_today_total(ory_id) + get_earned_total(ory_id)
        ├── github_strategy.get_active_wp(chat_id)  # 🔄 rows, ≤800 chars
        ├── build hermes_prompt (with WPs or fallback no-wp)
        └── gateway_mcp.hermes_chat(hermes_prompt, chat_id)
              └── rhythm_header + hermes_response → answer
```

## SLA

- Keep_typing активен на время `hermes_chat`
- T3+ ожидаемое время: 10–60с (зависит от hermes_chat latency)

## Ошибки

| Ситуация | Поведение |
|---------|-----------|
| `hermes_chat` упал / timeout | Rhythm header + «Сейчас недоступно. Попробуй позже.» |
| `active-wp.md` недоступен (GitHub 404 / нет токена) | Hermes отвечает без РП (промпт no-wp) |
| Нет `ory_id` (GitHub connect не настроен) | Ритм показывает 0/0, остальное работает |
| Markdown parse error | Ответ повторяется без `parse_mode="Markdown"` |
