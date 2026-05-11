---
family: C
type: scenario
commands: [tos, privacy]
tier_access: all
status: active
wp: WP-212 (B8.0)
---

# Сценарий 03-06: /tos и /privacy — юридические документы

## Описание

Команды типа C (микро): одна команда — одно сообщение.

| Команда | Описание |
|---------|---------|
| `/tos` | Условия использования IWE v0.1 |
| `/privacy` | Политика конфиденциальности IWE v0.1 |

## Поток

```
Пользователь: /tos
Бот: резюме ToS + ссылка https://system-school.ru/iwe/tos
```

## Условия доступа

Все тиры (T0–TD1). Доступны до авторизации.

## Файлы

- `handlers/legal.py` — обработчики
- `i18n/schema.yaml` — ключи `legal.tos_text`, `legal.privacy_text`
- Полные тексты: `DS-ecosystem-development/docs/legal/tos.md`, `privacy.md`
