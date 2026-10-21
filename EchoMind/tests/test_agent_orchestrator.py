import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from anthropic import AsyncAnthropic
from core.chat_events import event_sink

from agents.agent_orchestrator import (
    AgentProfile,
    AgentResponse,
    AgentType,
    AgentOrchestrator,
    BillingAgent,
    EscalationAgent,
    GeneralAgent,
    Request,
    ResponseComposer,
    RoutingDecision,
    TechnicalAgent,
    build_shared_rag_tools,
)
from core.intent_recognizer import IntentCategory, UrgencyLevel


def test_streaming_tool_round_trip_uses_real_sdk_and_emits_live_trace():
    events = []
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        tool_round = len(requests) == 1
        block = {"type": "tool_use", "id": "toolu_live", "name": "lookup_error_code", "input": {}} if tool_round else {"type": "text", "text": ""}
        delta = {"type": "input_json_delta", "partial_json": '{"error_code":"401"}'} if tool_round else {"type": "text_delta", "text": "请检查登录凭证。"}
        packets = [
            {"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant", "content": [], "model": "test-model", "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": block},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool_round else "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 10}},
            {"type": "message_stop"},
        ]
        body = "".join(f"event: {packet['type']}\ndata: {json.dumps(packet, ensure_ascii=False)}\n\n" for packet in packets)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    async def scenario():
        transport = httpx.MockTransport(provider)
        async with AsyncAnthropic(api_key="test-key", http_client=httpx.AsyncClient(transport=transport)) as client:
            with event_sink(events.append):
                return await TechnicalAgent(client, "test-model").handle(make_request())

    result = asyncio.run(scenario())
    assert result.success
    assert result.content == "请检查登录凭证。"
    assert result.tools_used == ["lookup_error_code"]
    assert all(request["stream"] for request in requests)
    assert requests[1]["messages"][-1]["content"][0]["type"] == "tool_result"
    tool_start = next(event for event in events if event["type"] == "tool_start")
    assert tool_start["input"] == {"error_code": "401"}
    assert next(event for event in events if event["type"] == "tool_end")["success"]
    assert [event["text"] for event in events if event["type"] == "text_delta"] == ["请检查登录凭证。"]


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

        class Messages:
            async def create(inner, **kwargs):
                self.calls.append(kwargs)
                if self.error:
                    raise self.error
                return self.response

        self.messages = Messages()


def make_request(**kwargs):
    values = {
        "message": "登录时报 401，同时这笔订单被重复扣款",
        "user_id": "u1",
        "conv_id": "c1",
        "intent": IntentCategory.TECHNICAL_LOGIN,
        "intent_group": "technical",
        "urgency": UrgencyLevel.HIGH,
        "intent_confidence": 0.92,
        "entities": {"error_code": ["401"], "amount": ["99 元"]},
    }
    values.update(kwargs)
    return Request(**values)


def test_agent_profiles_have_distinct_contracts_and_generation_config():
    assert isinstance(GeneralAgent.profile, AgentProfile)
    assert GeneralAgent.profile.role != TechnicalAgent.profile.role
    assert TechnicalAgent.profile.workflow != BillingAgent.profile.workflow
    assert TechnicalAgent.profile.temperature < GeneralAgent.profile.temperature
    assert "search_knowledge_base" in GeneralAgent.profile.tool_scope
    assert "lookup_error_code" in TechnicalAgent.profile.tool_scope
    assert "check_billing_fields" in BillingAgent.profile.tool_scope


@pytest.mark.parametrize("agent_class", [GeneralAgent, TechnicalAgent, BillingAgent])
def test_agent_output_budget_can_be_configured_at_call_time(agent_class, monkeypatch):
    client = FakeClient(response=SimpleNamespace(stop_reason="end_turn", content=[{"type": "text", "text": "完整回复"}]))
    agent = agent_class(client, "test-model")
    monkeypatch.setenv(f"ECHOMIND_{agent.agent_type.value.upper()}_MAX_TOKENS", "12000")
    result = asyncio.run(agent.handle(make_request()))
    assert result.success is True and result.content == "完整回复"
    assert client.calls[0]["max_tokens"] == 12000


@pytest.mark.parametrize("stop_reason, content", [
    ("max_tokens", [{"type": "text", "text": "半截回复"}]),
    ("max_tokens", [{"type": "tool_use", "id": "partial", "name": "lookup_error_code", "input": {"error_code": "401"}}]),
    ("end_turn", [{"type": "thinking", "thinking": "只有推理"}]),
    ("end_turn", [{"type": "text", "text": "  "}]),
])
def test_agent_rejects_incomplete_output_before_executing_tools(stop_reason, content):
    client = FakeClient(response=SimpleNamespace(stop_reason=stop_reason, content=content))
    agent = TechnicalAgent(client, "test-model")
    result = asyncio.run(agent.handle(make_request()))
    assert result.success is False and result.content != "半截回复"
    assert result.tools_used == [] and result.tool_traces == []
    assert agent.stats.success == 0
    assert len(client.calls) == 1


def test_truncated_composer_preserves_complete_specialist_results():
    client = FakeClient(response=SimpleNamespace(stop_reason="max_tokens", content=[{"type": "text", "text": "不完整汇总"}]))
    composer = ResponseComposer(client, "test-model")
    responses = [AgentResponse(AgentType.TECHNICAL, "完整技术排查", True), AgentResponse(AgentType.BILLING, "完整账单核验", True)]
    text = asyncio.run(composer.compose(make_request(), responses))
    assert "完整技术排查" in text and "完整账单核验" in text
    assert "不完整汇总" not in text


def test_domain_agents_build_different_role_packets():
    req = make_request()
    general_packet = GeneralAgent(FakeClient(), "test-model")._build_role_packet(req)
    technical_packet = TechnicalAgent(FakeClient(), "test-model")._build_role_packet(req)
    billing_packet = BillingAgent(FakeClient(), "test-model")._build_role_packet(req)

    assert "triage_targets" in general_packet
    assert "diagnostic_fields" in technical_packet
    assert "verification_fields" in billing_packet
    assert general_packet != technical_packet != billing_packet


def test_escalation_agent_is_a_real_non_llm_handoff_node():
    client = FakeClient()
    agent = EscalationAgent(client, "test-model")

    result = asyncio.run(agent.handle(make_request(
        intent=IntentCategory.HUMAN_HANDOFF,
        urgency=UrgencyLevel.CRITICAL,
    )))

    assert result.success is True
    assert result.escalate is True
    assert "人工升级" in result.content
    assert client.calls == []


def test_composer_fallback_preserves_primary_and_supporting_results():
    composer = ResponseComposer(FakeClient(error=RuntimeError("provider down")), "test-model")
    req = make_request()
    responses = [
        AgentResponse(AgentType.TECHNICAL, "先排查 Token 是否过期。", True),
        AgentResponse(AgentType.BILLING, "请提供两笔扣款的时间和金额。", True),
    ]

    content = asyncio.run(composer.compose(req, responses))

    assert content.startswith("先排查 Token 是否过期。")
    assert "补充说明" in content
    assert "两笔扣款" in content


def test_routing_decision_can_target_escalation_pool():
    # Keep this assertion close to the public data contract used by the API.
    decision = RoutingDecision(
        primary_agent=AgentType.ESCALATION,
        reason="critical request",
        confidence=1.0,
    )
    assert decision.agent_types == [AgentType.ESCALATION]
    assert not decision.multi_agent


def test_composite_request_routes_explicit_billing_signal_as_supporting_agent():
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator._pool = {
        AgentType.GENERAL: [object()],
        AgentType.TECHNICAL: [object()],
        AgentType.BILLING: [object()],
    }

    decision = orchestrator._route_decision(make_request())

    assert decision.primary_agent is AgentType.TECHNICAL
    assert decision.supporting_agents == [AgentType.BILLING]
    assert decision.multi_agent is True


def test_agent_tool_scopes_are_real_and_isolated():
    general_tools = set(GeneralAgent(FakeClient(), "test-model").get_tools())
    technical_tools = set(TechnicalAgent(FakeClient(), "test-model").get_tools())
    billing_tools = set(BillingAgent(FakeClient(), "test-model").get_tools())
    escalation_tools = set(EscalationAgent(FakeClient(), "test-model").get_tools())

    assert general_tools == {"inspect_request_context", "suggest_required_fields"}
    assert technical_tools == {"lookup_error_code", "build_diagnostic_plan"}
    assert billing_tools == {"check_billing_fields", "compare_amounts"}
    assert escalation_tools == {"create_handoff_summary"}
    assert not general_tools & technical_tools
    assert not technical_tools & billing_tools


def test_shared_rag_tool_is_available_to_all_agents():
    class RagManager:
        async def search_with_rewrite(self, tool_name, query, top_k=5):
            return type(
                "Result",
                (),
                {"success": True, "data": [{"title": "退款政策", "content": "7 天内可退款"}], "reranked": True},
            )()

    shared = build_shared_rag_tools(RagManager())

    general = GeneralAgent(FakeClient(), "test-model")
    technical = TechnicalAgent(FakeClient(), "test-model")
    billing = BillingAgent(FakeClient(), "test-model")
    escalation = EscalationAgent(FakeClient(), "test-model")

    for agent in (general, technical, billing, escalation):
        agent.set_shared_tools(shared)
        tools = agent.get_tools()
        assert "search_knowledge_base" in tools


def test_tool_input_validation_rejects_unknown_fields():
    agent = TechnicalAgent(FakeClient(), "test-model")
    spec = agent.get_tools()["lookup_error_code"]

    try:
        agent._validate_tool_input(spec, {"error_code": "401", "secret": "nope"})
    except ValueError as exc:
        assert "不允许的工具参数" in str(exc)
    else:
        raise AssertionError("unknown tool fields should be rejected")


def test_tool_use_round_trip_executes_only_whitelisted_tool():
    class ToolUseBlock:
        type = "tool_use"
        id = "toolu_1"
        name = "lookup_error_code"
        input = {"error_code": "401"}

    class TextBlock:
        type = "text"
        text = "已根据 401 错误码给出排查建议。"

    class ToolClient:
        def __init__(self):
            self.calls = []
            self.responses = [
                type("Response", (), {"content": [ToolUseBlock()]})(),
                type("Response", (), {"content": [TextBlock()]})(),
            ]

        class Messages:
            def __init__(self, owner):
                self.owner = owner

            async def create(self, **kwargs):
                self.owner.calls.append(kwargs)
                return self.owner.responses.pop(0)

        @property
        def messages(self):
            return self.Messages(self)

    client = ToolClient()
    agent = TechnicalAgent(client, "test-model")
    response = asyncio.run(agent.handle(make_request()))

    assert response.success is True
    assert response.tools_used == ["lookup_error_code"]
    assert len(response.tool_traces) == 1
    assert response.tool_traces[0]["tool_name"] == "lookup_error_code"
    assert response.tool_traces[0]["input"] == {"error_code": "401"}
    assert response.tool_traces[0]["success"] is True
    assert len(client.calls) == 2
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {
        "lookup_error_code",
        "build_diagnostic_plan",
    }
    assert "tool_result" in str(client.calls[1]["messages"])


def test_concurrent_requests_keep_tool_traces_isolated():
    class ToolUseBlock:
        type = "tool_use"

        def __init__(self, code):
            self.id = f"toolu_{code}"
            self.name = "lookup_error_code"
            self.input = {"error_code": code}

    class TextBlock:
        type = "text"

        def __init__(self, text):
            self.text = text

    class ConcurrentClient:
        class Messages:
            async def create(inner, **kwargs):
                messages = kwargs["messages"]
                last_content = messages[-1]["content"]
                if isinstance(last_content, list):
                    code = "500" if "500" in str(last_content) else "401"
                    await asyncio.sleep(0)
                    return type("Response", (), {"content": [TextBlock(f"{code} 排查完成")]})()
                code = "500" if "500" in str(last_content) else "401"
                await asyncio.sleep(0.01 if code == "401" else 0)
                return type("Response", (), {"content": [ToolUseBlock(code)]})()

        def __init__(self):
            self.messages = self.Messages()

    async def run_both():
        agent = TechnicalAgent(ConcurrentClient(), "test-model")
        return await asyncio.gather(
            agent.handle(make_request(message="登录报 401", entities={"error_code": ["401"]})),
            agent.handle(make_request(message="页面报 500", entities={"error_code": ["500"]})),
        )

    first, second = asyncio.run(run_both())
    assert first.tool_traces[0]["input"] == {"error_code": "401"}
    assert second.tool_traces[0]["input"] == {"error_code": "500"}


def test_monitor_penalty_falls_back_to_general_agent():
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    general = GeneralAgent(FakeClient(), "test-model")
    technical = TechnicalAgent(FakeClient(), "test-model")
    billing = BillingAgent(FakeClient(), "test-model")
    technical.stats.monitor_penalty = 0.5
    orchestrator._pool = {
        AgentType.GENERAL: [general],
        AgentType.TECHNICAL: [technical],
        AgentType.BILLING: [billing],
    }

    decision = orchestrator._route_decision(make_request(
        message="登录报 401",
        entities={"error_code": ["401"]},
    ))

    assert decision.primary_agent is AgentType.GENERAL
    assert decision.supporting_agents == []
    assert "monitor_fallback=[technical]" in decision.reason


@pytest.mark.parametrize("top_k", [0, True, 101, 2.5, "5"])
def test_shared_rag_rejects_invalid_top_k_without_calling_manager(top_k):
    class Unused:
        async def search_with_rewrite(self, *args, **kwargs):
            pytest.fail("invalid requests must not reach retrieval")
    from agents.tools import build_shared_rag_tools
    tool = build_shared_rag_tools(Unused())["search_knowledge_base"]
    result = asyncio.run(tool.handler(make_request(), {"query": "退款", "top_k": top_k}))
    assert result["success"] is False
