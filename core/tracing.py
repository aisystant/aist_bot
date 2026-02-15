"""
Трейсинг запросов — request-scoped traces с записью в Neon.

Архитектура:
- Каждый входящий message/callback → Trace (uuid, user_id, command, state)
- Внутри запроса — span("name") добавляет span к текущему trace
- В конце запроса — save_trace() записывает всё в таблицу request_traces
- ContextVar обеспечивает изоляцию между concurrent запросами

Использование:
    from core.tracing import span, start_trace, finish_trace

    # В middleware (автоматически):
    trace = start_trace(user_id=123, command="/mode", state="common.mode_select")
    ...
    await finish_trace(trace)

    # В компонентах:
    async with span("claude.api", max_tokens=2000):
        result = await claude.generate(...)
"""

import json
import time
import uuid
import logging
from contextvars import ContextVar
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional, List

logger = logging.getLogger(__name__)

# Request-scoped trace context
_current_trace: ContextVar[Optional['Trace']] = ContextVar('_current_trace', default=None)


@dataclass
class Span:
    """Одна операция внутри trace."""
    name: str
    start: float
    end: float = 0.0
    metadata: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return (self.end - self.start) * 1000


@dataclass
class Trace:
    """Полный trace одного запроса."""
    trace_id: str
    user_id: int
    command: str
    state: str
    spans: List[Span] = field(default_factory=list)
    start: float = field(default_factory=time.perf_counter)

    @property
    def total_ms(self) -> float:
        return (time.perf_counter() - self.start) * 1000


def start_trace(user_id: int, command: str, state: str) -> Trace:
    """Начать новый trace для текущего запроса."""
    trace = Trace(
        trace_id=uuid.uuid4().hex[:12],
        user_id=user_id,
        command=command,
        state=state,
    )
    _current_trace.set(trace)
    return trace


def get_current_trace() -> Optional[Trace]:
    """Получить текущий trace (если есть)."""
    return _current_trace.get()


@asynccontextmanager
async def span(name: str, **metadata):
    """Context manager для замера отдельной операции.

    Добавляет span к текущему trace (если есть).
    Если trace нет — просто логирует время.

    Usage:
        async with span("claude.api", max_tokens=2000):
            result = await claude.generate(...)
    """
    s = Span(name=name, start=time.perf_counter(), metadata=metadata)
    try:
        yield s
    finally:
        s.end = time.perf_counter()
        trace = _current_trace.get()
        if trace:
            trace.spans.append(s)
        if s.duration_ms > 1000:
            logger.info(f"[SPAN] {name}: {s.duration_ms:.0f}ms")
        else:
            logger.debug(f"[SPAN] {name}: {s.duration_ms:.0f}ms")


async def finish_trace(trace: Trace) -> None:
    """Завершить trace и записать в Neon."""
    total = trace.total_ms
    _current_trace.set(None)

    # Логируем summary
    spans_summary = ", ".join(
        f"{s.name}={s.duration_ms:.0f}ms" for s in trace.spans
    )
    logger.info(
        f"[TRACE] {trace.command} | {total:.0f}ms | "
        f"user={trace.user_id} state={trace.state} | {spans_summary}"
    )

    # Записываем в БД (fire-and-forget, ошибки не блокируют запрос)
    try:
        await _save_trace_to_db(trace)
    except Exception as e:
        logger.warning(f"[TRACE] Failed to save trace: {e}")


async def _save_trace_to_db(trace: Trace) -> None:
    """Записать trace в таблицу request_traces."""
    from db.connection import acquire

    spans_json = json.dumps([
        {"name": s.name, "duration_ms": round(s.duration_ms, 1), **s.metadata}
        for s in trace.spans
    ])

    async with await acquire() as conn:
        await conn.execute(
            """INSERT INTO request_traces
               (trace_id, user_id, command, state, total_ms, spans, created_at)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW())""",
            trace.trace_id,
            trace.user_id,
            trace.command[:100],
            trace.state,
            round(trace.total_ms, 1),
            spans_json,
        )
