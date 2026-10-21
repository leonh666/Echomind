"""SSE transport with heartbeats and cancellation of disconnected requests."""
import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, AsyncIterator, Awaitable, Callable, Dict

from core.chat_events import emit, event_sink

logger = logging.getLogger(__name__)


async def chat_event_stream(
    run: Callable[[], Awaitable[Dict[str, Any]]], heartbeat_seconds: float = 15.0,
) -> AsyncIterator[str]:
    queue: asyncio.Queue = asyncio.Queue()

    async def produce():
        with event_sink(queue.put_nowait):
            try:
                result = await run()
                emit("done", response=result)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Streaming chat request failed")
                emit("error", message="处理请求失败，请检查后端日志后重试。")
            finally:
                queue.put_nowait(None)

    task = asyncio.create_task(produce())
    try:
        yield ": connected\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if event is None:
                break
            yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
