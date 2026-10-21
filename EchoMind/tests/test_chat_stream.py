import asyncio
import json
from types import SimpleNamespace

from core.chat_events import emit, event_sink, suppress_answer
from core.chat_stream import chat_event_stream
from core.llm_utils import create_message


def payload(frame):
    return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))


def test_progress_and_text_arrive_before_request_finishes():
    async def scenario():
        release = asyncio.Event()

        async def run():
            emit("stage", message="正在检索")
            emit("text_delta", text="退款政策")
            await release.wait()
            return {"response": "退款政策"}

        stream = chat_event_stream(run)
        assert await anext(stream) == ": connected\n\n"
        assert payload(await anext(stream))["type"] == "stage"
        assert payload(await anext(stream))["text"] == "退款政策"
        release.set()
        assert payload(await anext(stream))["response"]["response"] == "退款政策"
        await stream.aclose()

    asyncio.run(scenario())


def test_closing_stream_cancels_inflight_work():
    async def scenario():
        cancelled = asyncio.Event()

        async def run():
            try:
                emit("stage", message="waiting")
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        stream = chat_event_stream(run)
        await anext(stream)
        await anext(stream)
        await stream.aclose()
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_parallel_requests_do_not_share_events():
    async def scenario():
        async def collect(label):
            async def run():
                await asyncio.sleep(0)
                emit("text_delta", text=label)
                return {"response": label}

            return [payload(frame) async for frame in chat_event_stream(run) if frame.startswith("event:")]

        first, second = await asyncio.gather(collect("first"), collect("second"))
        assert first[0]["text"] == "first"
        assert second[0]["text"] == "second"

    asyncio.run(scenario())


def test_errors_are_reported_without_exposing_provider_credentials():
    async def scenario():
        async def run():
            raise RuntimeError("provider failed with confidential-key")

        frames = [frame async for frame in chat_event_stream(run)]
        error = payload(frames[1])
        assert error["type"] == "error"
        assert "confidential-key" not in error["message"]

    asyncio.run(scenario())


def test_heartbeat_keeps_long_operations_connected():
    async def scenario():
        async def run():
            await asyncio.Event().wait()

        stream = chat_event_stream(run, heartbeat_seconds=0.001)
        await anext(stream)
        assert await anext(stream) == ": heartbeat\n\n"
        await stream.aclose()

    asyncio.run(scenario())


def test_llm_deltas_are_forwarded_as_generated_and_final_message_is_preserved():
    events = []
    final = SimpleNamespace(content=[{"type": "text", "text": "你好"}])

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def text_stream(self):
            async def chunks():
                yield "你"
                assert events[-1]["text"] == "你"
                yield "好"
            return chunks()

        async def get_final_message(self):
            return final

    class Messages:
        def stream(self, **kwargs):
            return Stream()

        async def create(self, **kwargs):
            raise AssertionError("streaming request used a buffered call")

    async def scenario():
        with event_sink(events.append):
            result = await create_message(SimpleNamespace(messages=Messages()), model="test")
        assert result is final

    asyncio.run(scenario())
    assert [event["type"] for event in events] == ["stage", "answer_start", "text_delta", "text_delta"]


def test_parallel_specialist_output_is_suppressed_but_tool_events_are_visible():
    events = []
    with event_sink(events.append):
        with suppress_answer():
            emit("answer_start")
            emit("text_delta", text="internal candidate")
            emit("tool_start", tool_name="search_knowledge_base")
        emit("text_delta", text="final response")
    assert [event["type"] for event in events] == ["tool_start", "text_delta"]
