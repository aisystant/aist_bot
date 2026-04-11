# Admin-команды

> Команды администратора бота — **не** для пользователей, **не** для отладки платформы. Служат для модерации и управления обратной связью. Доступны только в `DEVELOPER_CHAT_ID` (в текущей реализации admin совпадает с TD1, см. `CLAUDE.md § 12`).
>
> Отличие от [dev-commands](dev-commands.md): dev = тех. debug (latency, errors, health), admin = управление контентом и коммуникацией (feedback, сообщества).

---

## 1. `/reports` — триаж пользовательских bug-reports

**Файл:** [`handlers/feedback.py:109-165`](../handlers/feedback.py)

**Что делает:** Показывает агрегированный отчёт по `feedback_reports` (сообщения, отправленные пользователями через `/feedback`) с inline-кнопками фильтрации по периоду.

**Inline-кнопки:**

| Кнопка | Callback | Действие |
|--------|----------|----------|
| **Today (24h)** | `reports:day` | Фильтр `since_hours=24` |
| **This week (7d)** | `reports:week` | Фильтр `since_hours=168` |
| **All time** | `reports:all` | Без фильтра (по умолчанию) |
| **Clear** | `reports:clear` | `clear_all_reports()` — удаляет все записи |

**Формат вывода:** HTML отчёт с группировкой по `severity` (critical / high / medium / low) и `category` (bug / feature / other). При нажатии кнопки `edit_text` обновляет то же сообщение (inline sub-navigation pattern §10.9).

**Источники:** `feedback_reports` таблица, функция `_render_reports(since_hours, period_label)` из `handlers/feedback.py`.

**Правила:**
- Проверка `str(message.chat.id) != dev_chat_id` — silent return (не отвечает не-админу)
- `clear` — **деструктивная** операция, удаляет все reports. Используй осторожно.

**Использовать когда:** триаж новых bug-reports утром (по `day`), еженедельный обзор (`week`), решение «что решить / что удалить».

**Связанное:** [P-06 Observability](processes/process-06-observability.md) — отдельный слой error_logs (автоматические ошибки, не user-submitted).

---

## 2. `/community_report` — отчёт по чатам сообществ

**Файл:** [`handlers/workshop.py:437-...`](../handlers/workshop.py)

**Что делает:** Статистика по чатам «Семинар IWE» и «Мастерская IWE» за период (день или неделя). Показывает новых / ушедших / оплативших / бесплатных участников.

**Аргументы:**
- `/community_report` → период `week` (по умолчанию)
- `/community_report day` → за сутки
- `/community_report week` → за 7 дней

**Проверка прав:** `message.chat.id != DEVELOPER_CHAT_ID AND message.from_user.id != DEVELOPER_CHAT_ID` → silent return.

**Источники:**
- `SEMINAR_IWE_CHAT_ID`, `MASTERSKAYA_IWE_CHAT_ID` (env переменные)
- `get_community_stats(chat_id, days)` — агрегатор из Telegram chat members + оплаты

**Формат вывода:** HTML для каждого чата:
- Всего участников (+ новых за период)
- Оплативших / бесплатных
- Список новых за период (до 10):
  - `@username — оплата 3000₽` (если есть сумма)
  - `@username — добавлен админом` (если без суммы)
- Список ушедших (если есть)

**Использовать когда:** подготовка к Week Close, анализ роста сообществ, корреляция с маркетинговыми активностями.

---

## Соответствие коду и документации

При изменении `handlers/feedback.py` (в части `cmd_reports`/`cb_reports`) или `handlers/workshop.py` (в части `cmd_community_report`):
1. Найди затронутую команду в этом файле
2. Обнови раздел в том же коммите с кодом
3. НЕ создавай новый `scenario-*.md` — admin команды живут в одном файле (правило §4.2 CLAUDE.md)

---

## История изменений

| Дата | Изменение |
|------|-----------|
| 2026-04-11 | Создание документа. 2 admin-команды: `/reports` (триаж feedback), `/community_report` (отчёты по чатам IWE). |
