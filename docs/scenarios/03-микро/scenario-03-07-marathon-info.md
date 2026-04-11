# 03.07 `/marathon_info`

> Информационный экран про Марафон личного развития: что это, для кого, как начать.

---

## Обзор

| Параметр | Значение |
|----------|----------|
| Команда | `/marathon_info` |
| Вид | Микро (C) |
| Файл | [`handlers/info.py:84`](../../../handlers/info.py) |

---

## 1. Триггер и маршрут

- Пользователь вводит `/marathon_info` (или нажимает кнопку «Что это?» в `/about`) → `cmd_marathon_info`
- При переходе из inline-меню — `edit=True`, сообщение заменяется (inline sub-navigation)

## 2. Содержимое

**Текст:** i18n `t('info.marathon_*')` — описание марафона, длительность, темы, механика.

**Кнопки (tier-based):**

| Состояние | Кнопки |
|-----------|--------|
| `marathon_status='not_started'` | `[🚀 Начать марафон]` `[⚙️ Настройки]` |
| `marathon_status='active'` | `[📚 Продолжить]` `[⚙️ Настройки]` |
| `marathon_status='paused'` | `[▶️ Возобновить]` `[⚙️ Настройки]` |
| `marathon_status='completed'` | `[🔄 Начать заново]` `[Купить подписку]` |

## 3. Источники

| Что | Откуда |
|-----|--------|
| Статус марафона | `development.user_state.marathon_status` |
| Текст | i18n `info.marathon_intro`, `info.marathon_for_whom`, `info.marathon_how` |

## 4. Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `handlers/info.py` | `cmd_marathon_info` |
| `i18n/translations/*.yaml` | `info.marathon_*` |

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа (DOC1.C batch) |
