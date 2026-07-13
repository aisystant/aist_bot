"""
Langfuse integration — L5 Observability (WP-179 Ф3).

Dual-write: each trace/generation is sent to both Neon (existing) and Langfuse.
Graceful: if LANGFUSE_SECRET_KEY is unset or Langfuse is unreachable — everything
works as before.

SDK targeted: langfuse>=4 (OTEL-based client). The legacy .trace()/.span()/
.generation()/.score() methods were removed upstream in the v3 rewrite; this
module uses start_observation()/.update()/.score()/.end() instead.

Usage:
    from core.langfuse_client import langfuse_trace, langfuse_generation

    # In TracingMiddleware:
    lf_span = langfuse_trace(user_id=123, name="/learn", metadata={...})

    # In claude.py:
    langfuse_generation(
        name="generate_content",
        model="claude-sonnet-4-20250514",
        input=prompt,
        output=result,
        usage_details={"input": 500, "output": 1200},
    )
"""

import os
import logging
from typing import Optional, Any
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# (root span, propagate_attributes context manager) для текущего запроса
_current_lf_span: ContextVar[Optional[tuple]] = ContextVar('_current_lf_span', default=None)

# Singleton Langfuse client
_langfuse = None
_initialized = False


def _get_langfuse():
    """Lazy init Langfuse client. Returns None if not configured."""
    global _langfuse, _initialized

    if _initialized:
        return _langfuse

    _initialized = True

    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    host = os.getenv("LANGFUSE_HOST", "http://localhost:3000")

    if not secret_key or not public_key:
        logger.info("[Langfuse] Not configured (LANGFUSE_SECRET_KEY/PUBLIC_KEY missing). Observability disabled.")
        return None

    try:
        from langfuse import Langfuse
        _langfuse = Langfuse(
            secret_key=secret_key,
            public_key=public_key,
            host=host,
        )
        logger.info(f"[Langfuse] Connected to {host}")
        return _langfuse
    except Exception as e:
        logger.warning(f"[Langfuse] Init failed: {e}. Observability disabled.")
        return None


def langfuse_trace(
    user_id: int,
    name: str,
    trace_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> Optional[Any]:
    """Создать корневой Langfuse span (= trace) для текущего запроса.

    Вызывается из TracingMiddleware при начале обработки сообщения.
    Возвращает span object или None если Langfuse не настроен.
    """
    lf = _get_langfuse()
    if not lf:
        return None

    try:
        from langfuse import propagate_attributes
        from langfuse.types import TraceContext

        # Langfuse trace_id — 32-символьный hex; наш Neon trace_id короче,
        # поэтому используем его как seed для детерминированной привязки.
        lf_trace_id = lf.create_trace_id(seed=trace_id) if trace_id else None

        attr_ctx = propagate_attributes(
            user_id=str(user_id),
            session_id=session_id or str(user_id),
            trace_name=name,
            metadata=metadata or {},
        )
        attr_ctx.__enter__()

        span = lf.start_observation(
            name=name,
            as_type="span",
            trace_context=TraceContext(trace_id=lf_trace_id) if lf_trace_id else None,
        )
        _current_lf_span.set((span, attr_ctx))
        return span
    except Exception as e:
        logger.debug(f"[Langfuse] trace error: {e}")
        return None


def get_current_lf_trace() -> Optional[Any]:
    """Получить текущий корневой Langfuse span."""
    current = _current_lf_span.get()
    return current[0] if current else None


def langfuse_span(
    name: str,
    trace: Optional[Any] = None,
    metadata: Optional[dict] = None,
) -> Optional[Any]:
    """Создать дочерний span внутри текущего trace.

    Если trace не передан — берёт корневой span из ContextVar.
    """
    t = trace or get_current_lf_trace()
    if not t:
        return None

    try:
        return t.start_observation(
            name=name,
            as_type="span",
            metadata=metadata or {},
        )
    except Exception as e:
        logger.debug(f"[Langfuse] span error: {e}")
        return None


def langfuse_generation(
    name: str,
    model: str,
    input: Any = None,
    output: Any = None,
    usage_details: Optional[dict] = None,
    trace: Optional[Any] = None,
    metadata: Optional[dict] = None,
) -> Optional[Any]:
    """Записать LLM generation (Claude API call) в Langfuse.

    Вызывается из clients/claude.py после завершения API вызова.
    """
    t = trace or get_current_lf_trace()
    if not t:
        return None

    try:
        return t.start_observation(
            name=name,
            as_type="generation",
            model=model,
            input=input,
            output=output,
            usage_details=usage_details or {},
            metadata=metadata or {},
        )
    except Exception as e:
        logger.debug(f"[Langfuse] generation error: {e}")
        return None


def langfuse_score(
    name: str,
    value: float,
    trace: Optional[Any] = None,
    comment: Optional[str] = None,
) -> None:
    """Записать оценку качества в Langfuse (для L3 AI Quality feedback loop)."""
    t = trace or get_current_lf_trace()
    if not t:
        return

    try:
        t.score(
            name=name,
            value=value,
            comment=comment,
        )
    except Exception as e:
        logger.debug(f"[Langfuse] score error: {e}")


def langfuse_end_trace() -> None:
    """Завершить текущий Langfuse trace. Вызывается из TracingMiddleware."""
    current = _current_lf_span.get()
    _current_lf_span.set(None)

    if not current:
        return

    span, attr_ctx = current
    try:
        span.end()
    except Exception as e:
        logger.debug(f"[Langfuse] end trace error: {e}")
    finally:
        try:
            attr_ctx.__exit__(None, None, None)
        except Exception as e:
            logger.debug(f"[Langfuse] attribute context exit error: {e}")


def init_langfuse() -> None:
    """Eager init Langfuse at startup — eliminates ~600ms latency on first request."""
    _get_langfuse()


def langfuse_flush() -> None:
    """Flush Langfuse queue (при shutdown)."""
    lf = _get_langfuse()
    if lf:
        try:
            lf.flush()
        except Exception as e:
            logger.debug(f"[Langfuse] flush error: {e}")
