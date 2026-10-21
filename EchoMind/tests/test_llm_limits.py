import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import AsyncAnthropic

from core.intent_recognizer import IntentCategory, IntentRecognizer
from core.skill_loader import Skill, SkillManager
from evaluation.evaluator import LLMJudge
from mcp.tool_manager import MCPToolManager


def response(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[{"type": "text", "text": text}])


def client_for(resp):
    return SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=resp)))


@pytest.mark.parametrize("configured, expected", [(None, 4096), ("12000", 12000), ("0", 4096), ("-1", 4096), ("invalid", 4096)])
def test_intent_budget_and_full_recent_history_reach_real_sdk(monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv("ECHOMIND_INTENT_MAX_TOKENS", raising=False)
    else:
        monkeypatch.setenv("ECHOMIND_INTENT_MAX_TOKENS", configured)
    requests = []
    history = [{"role": "user", "content": f"历史{i}: " + "背景信息" * 100 + f"关键事实{i}"} for i in range(12)]

    def provider(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json={
            "id": "msg_intent", "type": "message", "role": "assistant", "model": "test-model",
            "content": [{"type": "text", "text": json.dumps({"intent": "refund", "confidence": 0.9})}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 30},
        })

    async def scenario():
        async with AsyncAnthropic(api_key="test-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider))) as client:
            recognizer = IntentRecognizer.__new__(IntentRecognizer)
            recognizer.client, recognizer.model = client, "test-model"
            return await recognizer._llm_recognize("申请退款", history)

    assert asyncio.run(scenario())["intent"] == IntentCategory.REFUND
    assert requests[0]["max_tokens"] == expected
    prompt = requests[0]["messages"][0]["content"]
    assert "关键事实2" in prompt and "关键事实11" in prompt
    assert "关键事实0" not in prompt and "关键事实1\n" not in prompt


def test_intent_cache_distinguishes_long_messages_and_history_suffixes():
    recognizer = IntentRecognizer.__new__(IntentRecognizer)
    prefix = "相同背景" * 100
    assert recognizer._cache_key(prefix + "退款") != recognizer._cache_key(prefix + "登录")
    history = [{"role": "user", "content": prefix + "退款"}]
    changed = [{"role": "user", "content": prefix + "登录"}]
    assert recognizer._cache_key("继续处理", history) != recognizer._cache_key("继续处理", changed)


@pytest.mark.parametrize("stop_reason, text", [
    ("max_tokens", '{"intent":"refund","confidence":0.9}'),
    ("end_turn", "  "),
])
def test_incomplete_intent_is_marked_failed_even_when_json_parses(stop_reason, text):
    recognizer = IntentRecognizer.__new__(IntentRecognizer)
    recognizer.client, recognizer.model = client_for(response(text, stop_reason)), "test-model"
    result = asyncio.run(recognizer._llm_recognize("申请退款", None))
    assert result["failed"] is True and result["intent"] == IntentCategory.OTHER


@pytest.mark.parametrize("stop_reason, text", [("max_tokens", '["不完整改写"]'), ("end_turn", " ")])
def test_incomplete_rewrite_preserves_original_query(stop_reason, text):
    manager = MCPToolManager.__new__(MCPToolManager)
    manager._client, manager._model = client_for(response(text, stop_reason)), "test-model"
    assert asyncio.run(manager.rewrite_query("原始完整问题")) == ["原始完整问题"]


@pytest.mark.parametrize("stop_reason, text", [("max_tokens", "[2,1,0]"), ("end_turn", " ")])
def test_incomplete_rerank_preserves_original_items(stop_reason, text):
    manager = MCPToolManager.__new__(MCPToolManager)
    manager._client, manager._model = client_for(response(text, stop_reason)), "test-model"
    items = [{"content": f"资料{i}"} for i in range(3)]
    assert asyncio.run(manager._rerank("查询", items, 2)) == items[:2]


def test_rerank_sees_facts_after_old_200_character_cutoff():
    manager = MCPToolManager.__new__(MCPToolManager)
    manager._client, manager._model = client_for(response("[1,0]")), "test-model"
    items = [{"content": "普通条款"}, {"content": "背景" * 600 + "关键条款：重复扣款需要两笔交易号"}]
    assert asyncio.run(manager._rerank("重复扣款", items, 1)) == [items[1]]
    prompt = manager._client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "关键条款：重复扣款需要两笔交易号" in prompt


def test_truncated_judge_json_is_marked_failed():
    client = client_for(response('{"relevance":1,"accuracy":1,"completeness":1,"helpfulness":1}', "max_tokens"))
    scores = asyncio.run(LLMJudge(client, "test-model").judge("退款", "退款说明"))
    assert scores.judge_failed is True
    assert "被截断" in scores.error


def test_long_skill_instructions_are_available_to_agent(tmp_path, monkeypatch):
    monkeypatch.delenv("ECHOMIND_SKILLS_MAX_PROMPT_CHARS", raising=False)
    monkeypatch.delenv("ECHOMIND_SKILL_MAX_CHARS", raising=False)
    manager = SkillManager(str(tmp_path))
    manager._skills = [
        Skill("退款流程", "", "背景说明" * 1200 + "重要退款核验要求", "refund.md"),
        Skill("登录流程", "", "背景说明" * 1200 + "重要登录排查要求", "login.md"),
    ]
    prompt = manager.prompt_for("登录失败和退款", "general")
    assert "重要退款核验要求" in prompt
    assert "重要登录排查要求" in prompt
