"""
Langfuse integration — L5 Observability (WP-179 Ф3).

Dual-write: каждый trace/span отправляется и в Neon (existing), и в Langfuse.
Graceful: если LANGFUSE_SECRET_KEY не задан или Langfuse недоступен — всё работает как раньше.

Использование:
    from core.langfuse_client import langfuse_trace, langfuse_span, langfuse_generation

    # В TracingMiddleware:
    lf_trace = langfuse_trace(user_id=123, name="/learn", metadata={...})

    # В claude.py:
    langfuse_generation(
        trace=lf_trace,
        name="generate_content",
        model="claude-sonnet-4-20250514",
        input=prompt,
        output=result,
        usage={"input": 500, "output": 1200},
    )
"""

import os
import logging
from typing import Optional, Any
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# Langfuse trace для текущего запроса (request-scoped)
_current_lf_trace: ContextVar[Optional[Any]] = ContextVar('_current_lf_trace', default=None)

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
    """Создать Langfuse trace для текущего запроса.

    Вызывается из TracingMiddleware при начале обработки сообщения.
    Возвращает trace object или None если Langfuse не настроен.
    """
    lf = _get_langfuse()
    if not lf:
        return None

    try:
        trace = lf.trace(
            id=trace_id,
            name=name,
            user_id=str(user_id),
            session_id=session_id or str(user_id),
            metadata=metadata or {},
        )
        _current_lf_trace.set(trace)
        return trace
    except Exception as e:
        logger.debug(f"[Langfuse] trace error: {e}")
        return None


def get_current_lf_trace() -> Optional[Any]:
    """Получить текущий Langfuse trace."""
    return _current_lf_trace.get()


def langfuse_span(
    name: str,
    trace: Optional[Any] = None,
    metadata: Optional[dict] = None,
) -> Optional[Any]:
    """Создать span внутри текущего trace.

    Если trace не передан — берёт из ContextVar.
    """
    t = trace or _current_lf_trace.get()
    if not t:
        return None

    try:
        return t.span(
            name=name,
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
    usage: Optional[dict] = None,
    trace: Optional[Any] = None,
    metadata: Optional[dict] = None,
) -> Optional[Any]:
    """Записать LLM generation (Claude API call) в Langfuse.

    Вызывается из clients/claude.py после завершения API вызова.
    """
    t = trace or _current_lf_trace.get()
    if not t:
        return None

    try:
        return t.generation(
            name=name,
            model=model,
            input=input,
            output=output,
            usage=usage or {},
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
    t = trace or _current_lf_trace.get()
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
    trace = _current_lf_trace.get()
    _current_lf_trace.set(None)

    if not trace:
        return

    try:
        trace.update(status_message="completed")
    except Exception as e:
        logger.debug(f"[Langfuse] end trace error: {e}")


def init_langfuse() -> None:
    """Eager init Langfuse at startup — eliminates ~600ms latency on first request."""
    _get_langfuse()


def langfuse_flush() -> None:
    """Flush Langfuse queue (при shutdown)."""
    lf = _get_langfuse()
    if lf:
        try:
            lf.flush()
        except Exception:
            pass
