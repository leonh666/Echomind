import asyncio
import json
import sys
from enum import Enum
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api import main
from core.chat_events import emit
from core.intent_recognizer import IntentCategory, UrgencyLevel
from agents.agent_orchestrator import AgentType


def configure(monkeypatch):
    class MsgRole(Enum):
        USER = "user"
        ASSISTANT = "assistant"

    monkeypatch.setitem(sys.modules, "memory.conversation_memory", SimpleNamespace(MsgRole=MsgRole))

    class Memory:
        def __init__(self):
            self.saved = []

        async def get_context(self, *args, **kwargs):
            return SimpleNamespace(recent_messages=[], to_prompt_text=lambda: "")

        async def add_message(self, *args):
            self.saved.append(args)

        async def update_profile(self, *args):
            pass

    class Orchestrator:
        async def recognize_intent(self, *args, **kwargs):
            return SimpleNamespace(intent=IntentCategory.REFUND, intent_group="billing",
                                   urgency=UrgencyLevel.LOW, confidence=0.9, entities={}, source_scores={})

        async def run(self, req):
            emit("text_delta", text="退款")
            await asyncio.sleep(0)
            emit("text_delta", text="政策")
            return SimpleNamespace(request_id=req.request_id, response="退款政策", intent=req.intent,
                agent_type=AgentType.BILLING, agent_types=[AgentType.BILLING], primary_agent=AgentType.BILLING,
                supporting_agents=[], tools_used=[], routing_reason="退款问题", routing_confidence=0.9,
                escalated=False, latency_ms=2)

    memory = Memory()
    monkeypatch.setattr(main, "_memory", memory)
    monkeypatch.setattr(main, "_orchestrator", Orchestrator())
    return memory


def test_stream_endpoint_preserves_session_final_response_and_memory(monkeypatch):
    memory = configure(monkeypatch)
    response = TestClient(main.app).post("/chat/stream", json={"message": "退款", "conv_id": "existing"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[0] == {"type": "session", "conv_id": "existing"}
    assert [event["text"] for event in events if event["type"] == "text_delta"] == ["退款", "政策"]
    assert events[-1]["type"] == "done"
    assert events[-1]["response"]["response"] == "退款政策"
    assert events[-1]["response"]["conv_id"] == "existing"
    assert [entry[-1] for entry in memory.saved] == ["退款", "退款政策"]


def test_original_chat_endpoint_still_returns_complete_json(monkeypatch):
    configure(monkeypatch)
    response = TestClient(main.app).post("/chat", json={"message": "退款"})
    assert response.status_code == 200
    assert response.json()["response"] == "退款政策"
    assert response.json()["agent_type"] == "billing"


def test_unavailable_service_returns_http_error_before_opening_stream(monkeypatch):
    monkeypatch.setattr(main, "_memory", None)
    response = TestClient(main.app).post("/chat/stream", json={"message": "退款"})
    assert response.status_code == 503
