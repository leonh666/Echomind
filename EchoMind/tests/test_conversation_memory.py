import asyncio
import copy
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import AsyncAnthropic

# 这些测试不初始化 ChromaDB；与知识库测试一样，用内存集合验证写入边界。
try:
    import chromadb  # noqa: F401
except ModuleNotFoundError:
    sys.modules.setdefault("chromadb", types.ModuleType("chromadb"))

from memory.conversation_memory import MemoryContext, MemoryManager, Message, MsgRole


OLD_PROFILE = {"preferences": ["希望使用简单的话"], "entities": {"产品": ["Chrome"], "问题类型": ["登录失败"]}}
NEW_PROFILE = {"preferences": ["希望使用简单的话", "希望获得分步指导"], "entities": {"产品": ["Chrome"], "问题类型": ["登录失败", "重复扣款"]}}
DOC_ID = "user_profile:user-1"


class ProfileCollection:
    def __init__(self, existing=True, read_error=None, write_error=None):
        self.records = {DOC_ID: {"document": json.dumps(OLD_PROFILE, ensure_ascii=False), "metadata": {"user_id": "user-1", "conv_id": "old-conv"}}} if existing else {}
        self.read_error = read_error
        self.write_error = write_error
        self.upserts = []
        self.deletes = []

    def get(self, ids=None, where=None):
        if self.read_error:
            raise self.read_error
        records = [self.records[doc_id] for doc_id in (ids or self.records) if doc_id in self.records]
        return {"documents": [record["document"] for record in records], "metadatas": [record["metadata"] for record in records]}

    def delete(self, ids):
        self.deletes.append(ids)
        for doc_id in ids:
            self.records.pop(doc_id, None)

    def upsert(self, **kwargs):
        self.upserts.append(kwargs)
        if self.write_error:
            raise self.write_error
        for doc_id, document, metadata in zip(kwargs["ids"], kwargs["documents"], kwargs["metadatas"]):
            self.records[doc_id] = {"document": document, "metadata": metadata}


def response(raw=None, *, stop_reason="end_turn", content=None):
    if content is None:
        content = [{"type": "text", "text": raw or ""}]
    return SimpleNamespace(stop_reason=stop_reason, content=content)


def make_manager(resp=None, **collection_kwargs):
    manager = MemoryManager.__new__(MemoryManager)
    manager._model = "test-model"
    manager._profile = ProfileCollection(**collection_kwargs)
    manager._client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=resp)))
    manager._get_working_memory = AsyncMock(return_value=[
        Message(role=MsgRole.USER, content="我不太懂技术，请用简单的话分步骤告诉我。"),
        Message(role=MsgRole.ASSISTANT, content="先退出再重新登录。"),
    ])
    return manager


@pytest.mark.parametrize("existing", [True, False], ids=["update-existing", "create-new"])
def test_complete_profile_uses_real_sdk_and_is_available_for_next_read(existing):
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_profile", "type": "message", "role": "assistant", "model": "test-model",
            "content": [{"type": "text", "text": json.dumps(NEW_PROFILE, ensure_ascii=False)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 80},
        })

    manager = make_manager(existing=existing)

    async def scenario():
        async with AsyncAnthropic(api_key="test-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider))) as client:
            manager._client = client
            await manager.update_profile("user-1", "new-conv")
        return await manager._get_profile("user-1")

    assert asyncio.run(scenario()) == NEW_PROFILE
    assert len(requests) == 1
    assert requests[0]["max_tokens"] == 4096
    prompt = requests[0]["messages"][0]["content"]
    assert "user: 我不太懂技术" in prompt
    assert json.dumps(OLD_PROFILE, ensure_ascii=False) in prompt if existing else "已有画像:\n{}" in prompt
    assert manager._profile.deletes == []
    assert len(manager._profile.upserts) == 1
    stored = manager._profile.records[DOC_ID]
    assert stored["metadata"]["user_id"] == "user-1"
    assert stored["metadata"]["conv_id"] == "new-conv"
    assert stored["metadata"]["updated_at"]


@pytest.mark.parametrize("resp", [
    response(content=[{"type": "thinking", "thinking": "推理"}, {"type": "text", "text": json.dumps(NEW_PROFILE)}]),
    response(content=[{"type": "text", "text": '{"preferences": []'}, {"type": "text", "text": ', "entities": {"产品": [], "问题类型": []}}'}]),
    response("```json\n" + json.dumps(NEW_PROFILE) + "\n```"),
    response(json.dumps({"preferences": [], "entities": {"产品": [], "问题类型": []}})),
], ids=["thinking-plus-text", "multiple-text-blocks", "complete-json-fence", "empty-lists"])
def test_supported_complete_json_is_saved(resp):
    manager = make_manager(resp)
    asyncio.run(manager.update_profile("user-1", "new-conv"))
    assert len(manager._profile.upserts) == 1
    assert manager._profile.deletes == []


@pytest.mark.parametrize("resp, reason", [
    (response(json.dumps(NEW_PROFILE), stop_reason="max_tokens"), "被截断"),
    (response(stop_reason="max_tokens", content=[{"type": "thinking", "thinking": "只有推理，没有 JSON"}]), "被截断"),
    (response(content=[{"type": "thinking", "thinking": "只有推理"}]), "没有文本"),
    (response("  \n\t"), "没有文本"),
    (response(json.dumps(NEW_PROFILE), stop_reason="tool_use"), "未正常结束"),
    (response(json.dumps(NEW_PROFILE), stop_reason=None), "未正常结束"),
    (response('{"preferences": ['), "Expecting"),
    (response("先说明一下：" + json.dumps(NEW_PROFILE)), "Expecting"),
    (response("```json\n" + json.dumps(NEW_PROFILE)), "代码块"),
    (response("[]"), "JSON 对象"),
    (response("null"), "JSON 对象"),
    (response('{"preferences": []}'), "JSON 对象"),
    (response(json.dumps({**NEW_PROFILE, "unexpected": "value"})), "JSON 对象"),
    (response(json.dumps({**NEW_PROFILE, "preferences": "简单的话"})), "preferences"),
    (response(json.dumps({**NEW_PROFILE, "preferences": [42]})), "preferences"),
    (response(json.dumps({**NEW_PROFILE, "preferences": [" "]})), "preferences"),
    (response(json.dumps({**NEW_PROFILE, "entities": []})), "entities"),
    (response(json.dumps({**NEW_PROFILE, "entities": {"产品": []}})), "entities"),
    (response(json.dumps({**NEW_PROFILE, "entities": {"产品": [], "问题类型": [], "其他": []}})), "entities"),
    (response(json.dumps({**NEW_PROFILE, "entities": {"产品": [False], "问题类型": []}})), "entities.产品"),
    (response(json.dumps({**NEW_PROFILE, "entities": {"产品": [], "问题类型": None}})), "entities.问题类型"),
], ids=["truncated-valid-json", "recorded-thinking-only-truncation", "thinking-without-text", "blank-text", "tool-stop", "unknown-stop", "invalid-json", "prose-before-json", "incomplete-fence", "root-list", "root-null", "missing-field", "extra-field", "preferences-not-list", "preferences-not-strings", "blank-preference", "entities-not-object", "missing-entity-field", "extra-entity-field", "product-not-strings", "issue-not-list"])
def test_invalid_model_output_preserves_old_profile_without_writing(resp, reason, caplog):
    manager = make_manager(resp)
    before = copy.deepcopy(manager._profile.records)
    asyncio.run(manager.update_profile("user-1", "new-conv"))
    assert manager._profile.records == before
    assert manager._profile.upserts == []
    assert manager._profile.deletes == []
    assert asyncio.run(manager._get_profile("user-1")) == OLD_PROFILE
    assert "保留旧画像" in caplog.text
    assert reason in caplog.text


def test_database_write_failure_preserves_old_document_and_metadata(caplog):
    manager = make_manager(response(json.dumps(NEW_PROFILE)), write_error=RuntimeError("embedding failed"))
    before = copy.deepcopy(manager._profile.records)
    asyncio.run(manager.update_profile("user-1", "new-conv"))
    assert manager._profile.records == before
    assert manager._profile.deletes == []
    assert len(manager._profile.upserts) == 1
    assert "embedding failed" in caplog.text
    assert "保留旧画像" in caplog.text


def test_provider_failure_preserves_old_profile(caplog):
    manager = make_manager()
    manager._client.messages.create.side_effect = RuntimeError("provider unavailable")
    before = copy.deepcopy(manager._profile.records)
    asyncio.run(manager.update_profile("user-1", "new-conv"))
    assert manager._profile.records == before
    assert manager._profile.upserts == []
    assert manager._profile.deletes == []
    assert "provider unavailable" in caplog.text


def test_read_failure_does_not_overwrite_profile_based_on_empty_context(caplog):
    manager = make_manager(response(json.dumps(NEW_PROFILE)), read_error=RuntimeError("read unavailable"))
    before = copy.deepcopy(manager._profile.records)
    asyncio.run(manager.update_profile("user-1", "new-conv"))
    manager._client.messages.create.assert_not_awaited()
    assert manager._profile.records == before
    assert manager._profile.upserts == []
    assert manager._profile.deletes == []
    assert "read unavailable" in caplog.text
    # 供正常聊天读取时，仍保留原来的容错行为。
    assert asyncio.run(manager._get_profile("user-1")) == {}


def test_no_messages_does_not_call_model_or_change_old_profile():
    manager = make_manager()
    manager._get_working_memory.return_value = []
    before = copy.deepcopy(manager._profile.records)
    asyncio.run(manager.update_profile("user-1", "new-conv"))
    manager._client.messages.create.assert_not_awaited()
    assert manager._profile.records == before
    assert manager._profile.upserts == []


@pytest.mark.parametrize("resp", [
    response("半截摘要", stop_reason="max_tokens"),
    response("  "),
    response(content=[{"type": "thinking", "thinking": "只有推理"}]),
])
def test_incomplete_summary_does_not_delete_or_replace_working_memory(resp, caplog):
    manager = make_manager(resp)
    manager._get_working_memory.return_value = [Message(MsgRole.USER, f"重要事实{i}") for i in range(15)]
    manager._redis = AsyncMock()
    manager._store_episodic = AsyncMock()
    asyncio.run(manager._compress("user-1", "conv-1"))
    assert manager._redis.mock_calls == []
    manager._store_episodic.assert_not_awaited()
    assert "保留工作记忆" in caplog.text


def test_long_summary_and_profile_input_retain_earlier_facts():
    manager = make_manager(response(json.dumps(NEW_PROFILE)))
    manager._get_working_memory.return_value = [Message(MsgRole.USER, f"重要事实{i}") for i in range(20)]
    asyncio.run(manager.update_profile("user-1", "new-conv"))
    prompt = manager._client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "重要事实0\n" in prompt and "重要事实19" in prompt
    long_summary = "背景" * 600 + "重要的支付约束"
    assert asyncio.run(manager._merge_summary("", long_summary)) == long_summary


def test_truncated_summary_merge_preserves_old_and_new_facts():
    manager = make_manager(response("不完整合并", stop_reason="max_tokens"))
    merged = asyncio.run(manager._merge_summary("旧摘要：用户已经重新登录", "新摘要：仍然报错401"))
    assert "用户已经重新登录" in merged and "仍然报错401" in merged
    assert "不完整合并" not in merged


def test_memory_context_includes_earlier_messages_and_all_retrieved_history():
    context = MemoryContext(
        recent_messages=[Message(MsgRole.USER, f"事实{i}") for i in range(20)],
        relevant_history=[f"相关历史{i}" for i in range(5)],
        user_profile=OLD_PROFILE,
        summary="旧会话摘要",
    ).to_prompt_text()
    assert "user: 事实0\n" in context and "user: 事实19" in context
    assert "相关历史4" in context and "用户画像" in context and "旧会话摘要" in context
