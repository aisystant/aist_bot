# 03.20 `/wakatime`

> WakaTime OAuth подключение: статус, connect, disconnect.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/wakatime` |
| Вид | Микро (C) |
| Файл | [`handlers/wakatime.py:34`](../../../handlers/wakatime.py) |
| Subcommand | `/wakatime disconnect` |
| Связанное | [`/waka`](scenario-03-19-waka.md) — просмотр статистики |

---

## 1. Триггер и ветки

| Состояние | Что видит user |
|-----------|----------------|
| Не подключён | Кнопка `[🔗 Подключить WakaTime]` → OAuth |
| Подключён | Аккаунт + кнопка `[Отключить]` + ссылка «Смотреть статистику /waka» |
| `/wakatime disconnect` | Очистка токена |

## 2. OAuth flow

1. `/wakatime` или настройки → `await wakatime_oauth.get_authorization_url()`
2. Сохранение state в `public.oauth_pending_state` (`provider='wakatime'`, БД secrets).
   Ссылка действует 10 минут и переживает перезапуск бота.
3. Redirect на WakaTime consent
4. Callback однократно проверяет и удаляет state через `await validate_state()`.
   Неизвестный, просроченный или уже использованный state отклоняется до обмена кода.
5. Callback обменивает код и сохраняет токены в `persona.user_integrations`.

После сообщения «Сессия авторизации истекла» нужно запросить новую ссылку в боте.

## 3. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/wakatime.py` | `cmd_wakatime` + disconnect |
| `clients/wakatime_oauth.py` | OAuth flow |
| `clients/wakatime.py` | API calls (используется в `/waka`) |

## 4. Связанное с IWE

См. документ `setup-wakatime` скилл в `~/IWE/.claude/skills/` — инструкция настройки WakaTime для Claude Code и VS Code. Отдельная тема от бота, но общий API key может переиспользоваться.

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
