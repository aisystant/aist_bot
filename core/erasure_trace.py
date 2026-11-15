"""Команды удаления данных Аналитика чата не хранят номер человека.

WP-612. Две команды и четыре кнопки подтверждения: время, имя действия и
результат остаются, номер человека в эти записи не попадает. Обычные команды
и отметка «был активен сегодня» не меняются.

Ноль в колонке user_id — не человек. У Telegram нет пользователя с номером 0.
"""

from __future__ import annotations

import re

# Совпадают с handlers/chat_analyst.py (ERASE_* / RESUME_*). Префикс cb: —
# это форма, которую TracingMiddleware кладёт в журнал.
ERASURE_TRACE_COMMANDS = frozenset({
    "/erase_me",
    "/resume_me",
    "cb:chat_analyst:erase:confirm",
    "cb:chat_analyst:erase:cancel",
    "cb:chat_analyst:resume:confirm",
    "cb:chat_analyst:resume:cancel",
})

# Числовая колонка не принимает пусто. 0 не является номером Telegram.
OMITTED_USER_ID = 0

# В текстовых полях сессии вместо названия действия.
REDACTED_POINT = "скрыто"

_COMMAND_EXPR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(->>'[A-Za-z0-9_]+')?$")


def canonical_trace_command(command: str) -> str:
    """Первое слово команды. У /команды@бот отбрасывается имя бота."""
    token = (command or "").split()[0] if command else ""
    if token.startswith("/") and "@" in token:
        token = token.split("@", 1)[0]
    return token


def is_erasure_trace_command(command: str) -> bool:
    return canonical_trace_command(command) in ERASURE_TRACE_COMMANDS


def redact_command_list(commands: list[str]) -> list[str]:
    """Убрать шесть действий из списка команд сессии, остальное сохранить."""
    return [item for item in commands if not is_erasure_trace_command(item)]


def redact_entry_point(value: str | None) -> str | None:
    if value is None:
        return None
    if is_erasure_trace_command(value):
        return REDACTED_POINT
    return value


def erasure_command_list() -> list[str]:
    return sorted(ERASURE_TRACE_COMMANDS)


def fill_sql(template: str, **parts: str) -> str:
    """Подставить доверенные фрагменты в текст запроса.

    Маркер в тексте — %%ИМЯ%%. Значение собирает вызывающий код из белого
    списка, не из текста человека. Это обычная замена, не f-строка: проверка
    безопасности не принимает маркер за имя таблицы.
    """
    rendered = template
    for key, value in parts.items():
        token = f"%%{key}%%"
        if token not in rendered:
            raise KeyError(key)
        rendered = rendered.replace(token, value)
    return rendered


def health_count_sql() -> dict[str, str]:
    """Подсчёт оставшихся связок в базе наблюдаемости. Ключ — место, значение — SQL."""
    omitted = str(OMITTED_USER_ID)
    return {
        "request_traces": fill_sql(
            """
            SELECT command, count(*)::int AS rows
            FROM public.request_traces
            WHERE %%MATCH%%
              AND user_id IS DISTINCT FROM %%OMITTED%%
            GROUP BY command
            ORDER BY command
            """,
            MATCH=sensitive_match_sql("command"),
            OMITTED=omitted,
        ),
        "user_sessions.commands": fill_sql(
            """
            SELECT elem AS command, count(*)::int AS rows
            FROM public.user_sessions AS s,
                 jsonb_array_elements_text(s.commands) AS elem
            WHERE %%MATCH%%
            GROUP BY elem
            ORDER BY elem
            """,
            MATCH=sensitive_match_sql("elem"),
        ),
        "user_sessions.entry_point": fill_sql(
            """
            SELECT entry_point AS command, count(*)::int AS rows
            FROM public.user_sessions
            WHERE %%MATCH%%
            GROUP BY entry_point
            ORDER BY entry_point
            """,
            MATCH=sensitive_match_sql("entry_point"),
        ),
        "user_sessions.exit_point": fill_sql(
            """
            SELECT exit_point AS command, count(*)::int AS rows
            FROM public.user_sessions
            WHERE %%MATCH%%
            GROUP BY exit_point
            ORDER BY exit_point
            """,
            MATCH=sensitive_match_sql("exit_point"),
        ),
        "error_logs": fill_sql(
            """
            SELECT context->>'command' AS command, count(*)::int AS rows
            FROM public.error_logs
            WHERE jsonb_typeof(context) = 'object'
              AND %%MATCH%%
              AND COALESCE(context->>'user_id', '0') <> '%%OMITTED%%'
            GROUP BY 1
            ORDER BY 1
            """,
            MATCH=sensitive_match_sql("context->>'command'"),
            OMITTED=omitted,
        ),
    }


def bot_count_sql() -> dict[str, str]:
    """Журнал бота: событие начала сессии хранит название команды."""
    return {
        "user_events.session_start": fill_sql(
            """
            SELECT payload->>'entry_point' AS command, count(*)::int AS rows
            FROM development.user_events
            WHERE event_type = 'session_start'
              AND %%MATCH%%
            GROUP BY 1
            ORDER BY 1
            """,
            MATCH=sensitive_match_sql("payload->>'entry_point'"),
        ),
    }


def learning_count_sql() -> dict[str, str]:
    """Центральный журнал. session_start здесь название команды удаления не несёт.

    Запрос session_start_mentions только проверяет это. Ненулевое число
    останавливает чистку журнала: правка payload ломает цепочку хэшей.
    """
    return {
        "domain_event.request_traced": fill_sql(
            """
            SELECT payload->>'command' AS command, count(*)::int AS rows
            FROM public.domain_event
            WHERE event_type = 'request_traced'
              AND account_id IS NOT NULL
              AND %%MATCH%%
            GROUP BY 1
            ORDER BY 1
            """,
            MATCH=sensitive_match_sql("payload->>'command'"),
        ),
        "domain_event.session_start_mentions": """
            SELECT count(*)::int AS rows
            FROM public.domain_event
            WHERE event_type = 'session_start'
              AND (
                    payload::text LIKE '%/erase_me%'
                 OR payload::text LIKE '%/resume_me%'
                 OR payload::text LIKE '%chat_analyst:erase%'
                 OR payload::text LIKE '%chat_analyst:resume%'
              )
        """,
    }


def sensitive_match_sql(expression: str) -> str:
    """Условие «это одно из шести действий». expression — доверенный SQL-скаляр.

    Список команд передаётся параметром $1, не склейкой в текст запроса.
    Суффикс @бот покрывает старые строки, куда команда попала с именем бота.
    """
    if not _COMMAND_EXPR.fullmatch(expression):
        raise ValueError(f"unsafe SQL expression: {expression!r}")
    return (
        f"({expression} = ANY($1::text[]) "
        f"OR {expression} LIKE '/erase_me@%' "
        f"OR {expression} LIKE '/resume_me@%')"
    )
