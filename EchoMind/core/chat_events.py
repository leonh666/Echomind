"""Request-scoped chat events; parallel requests never share a callback."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional


_sink: ContextVar[Optional[Callable[[Dict[str, Any]], None]]] = ContextVar("chat_event_sink", default=None)
_answer_enabled: ContextVar[bool] = ContextVar("chat_answer_enabled", default=True)


def emit(event_type: str, **data: Any) -> None:
    sink = _sink.get()
    if sink is not None:
        if event_type in {"answer_start", "text_delta"} and not _answer_enabled.get():
            return
        sink({"type": event_type, **data})


def streaming_enabled() -> bool:
    return _sink.get() is not None and _answer_enabled.get()


@contextmanager
def event_sink(callback: Callable[[Dict[str, Any]], None]):
    token = _sink.set(callback)
    try:
        yield
    finally:
        _sink.reset(token)


@contextmanager
def suppress_answer():
    """Parallel specialists publish tool progress; only the composer publishes text."""
    token = _answer_enabled.set(False)
    try:
        yield
    finally:
        _answer_enabled.reset(token)
