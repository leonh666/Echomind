"""A changing model answer must not mutate a business handling decision."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from agents.agent_orchestrator import (AgentOrchestrator, AgentResponse, AgentType,
    BillingAgent, Request, RoutingDecision, escalation_reasons)
from core.intent_recognizer import IntentCategory, UrgencyLevel
from test_agent_orchestrator import FakeClient
from test_operations_api import service, configure_chat
from api import main


def request(**kwargs):
    return Request(**{'message': '我想了解退款政策和申请流程', 'user_id': 'test', 'conv_id': 'conv',
                      'intent': IntentCategory.REFUND, 'urgency': UrgencyLevel.LOW, **kwargs})


@pytest.mark.parametrize('answer', ['请核对退款规则。', '如有疑问可联系人工客服。', '暂不需要转人工。', 'specialist can help; do not escalate', '无法处理实际退款，但可以介绍申请流程。'])
def test_refund_inquiry_status_does_not_depend_on_answer_wording(answer):
    client = FakeClient(response=SimpleNamespace(stop_reason='end_turn', content=[{'type': 'text', 'text': answer}]))
    result = asyncio.run(BillingAgent(client, 'test-model').handle(request()))
    assert result.success and result.content == answer
    assert result.escalate is False


@pytest.mark.parametrize('parallel', [False, True])
@pytest.mark.parametrize('facts', [{'intent': IntentCategory.HUMAN_HANDOFF}, {'urgency': UrgencyLevel.CRITICAL}])
def test_parallel_and_single_paths_apply_the_same_request_policy(parallel, facts):
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    decision = RoutingDecision(AgentType.BILLING, [AgentType.TECHNICAL] if parallel else [])
    orchestrator._route_decision = lambda req: decision
    orchestrator._record_tool_trace = lambda result: None
    # Even a specialist response that omits escalation cannot bypass request facts.
    orchestrator._execute = AsyncMock(return_value=AgentResponse(AgentType.BILLING, '核验信息', True, escalate=False))
    orchestrator._composer = SimpleNamespace(compose=AsyncMock(return_value='汇总信息'))
    req = request(**facts)
    result = asyncio.run(orchestrator.run(req))
    assert result.escalated
    assert result.escalation_reasons == escalation_reasons(req)


def test_chat_policy_inquiry_saves_no_draft_but_explicit_handoff_saves_pending_only(service, monkeypatch):
    client, store, _, _, _, headers = service
    configure_chat(monkeypatch)
    original = main._orchestrator.run
    async def run(req):
        result = await original(req)
        result.escalated = req.message == '请转人工'
        result.escalation_reasons = ['请求被识别为人工升级意图'] if result.escalated else []
        return result
    monkeypatch.setattr(main._orchestrator, 'run', run)
    auth = headers('customer')
    plain = client.post('/chat', json={'message': '退款政策'}, headers=auth).json()
    assert plain['handoff_status'] == 'not_requested' and plain['tasks'] == []
    human = client.post('/chat', json={'message': '请转人工'}, headers=auth).json()
    assert human['handoff_status'] == 'pending_confirmation'
    assert human['escalation_reasons'] == ['请求被识别为人工升级意图']
    assert human['tasks'][0]['status'] == 'waiting_confirmation'
    assert client.get('/tickets', headers=auth).json()['items'] == []
    no = client.post('/chat', json={'message': '请创建工单，但不要创建工单'}, headers=auth).json()
    assert no['tasks'] == []
