import asyncio
import json
from types import SimpleNamespace

import pytest

from mcp.tool_manager import MCPToolManager, Tool


def manager(replies=()):
    instance = MCPToolManager.__new__(MCPToolManager)
    instance._model = "fake"
    instance._tools, instance._cache = {}, {}
    queue = list(replies)
    async def create(**kwargs):
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply))])
    instance._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return instance


def candidate(identifier, score=0):
    return {"chunk_id": identifier, "content": "退款规则 " + identifier, "score": score}


def register(instance, handler, **kwargs):
    instance.register(Tool(name="search", description="test", handler=handler,
                           schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                           supports_rerank=True, **kwargs))


def test_rerank_status_is_skipped_when_no_rerank_was_needed():
    instance = manager()
    register(instance, lambda p, c: [candidate("a")])
    result = asyncio.run(instance.search_with_rewrite("search", "退款", rewrite=False))
    assert result.success and not result.reranked
    assert result.rerank_status == "skipped" and result.rewrite_status == "skipped"


@pytest.mark.parametrize("order", [[0, 0], [True, 1], [0, 8], [0], "invalid"])
def test_invalid_rerank_response_reports_failure_without_losing_recall(order):
    instance = manager([order])
    register(instance, lambda p, c: [candidate("a"), candidate("b")])
    result = asyncio.run(instance.search_with_rewrite("search", "退款", top_k=1, rewrite=False))
    assert result.success and result.data[0]["chunk_id"] == "a"
    assert not result.reranked and result.rerank_status == "failed" and result.rerank_error


def test_valid_rerank_and_stable_deduplication():
    instance = manager([[1, 0]])
    register(instance, lambda p, c: [candidate("a", 1), candidate("a", 7), candidate("b", 2)])
    result = asyncio.run(instance.search_with_rewrite("search", "退款", top_k=1, rewrite=False))
    assert result.reranked and result.data[0]["chunk_id"] == "b"


def test_partial_failure_is_distinct_from_a_fully_healthy_search():
    instance = manager([["另一个查询"]])
    def search(params, context):
        if params["query"] == "另一个查询":
            raise RuntimeError("offline")
        return [candidate("a")]
    register(instance, search)
    result = asyncio.run(instance.search_with_rewrite("search", "退款"))
    assert result.success and result.partial_failure and result.error


def test_fallback_is_failure_and_never_reused_as_healthy_cache():
    instance = manager()
    def fail(p, c):
        raise RuntimeError("offline")
    register(instance, fail, cache_ttl=60, fallback=lambda p, c, e: [candidate("fallback")])
    result = asyncio.run(instance.call("search", {"query": "退款"}))
    assert not result.success and result.fallback
    assert not instance._cache
    merged = asyncio.run(instance.search_with_rewrite("search", "退款", rewrite=False))
    assert not merged.success and merged.data == []


def test_empty_healthy_result_differs_from_backend_failure():
    instance = manager()
    register(instance, lambda p, c: [])
    result = asyncio.run(instance.search_with_rewrite("search", "不存在", rewrite=False))
    assert result.success and result.data == [] and not result.partial_failure


def test_cache_is_scoped_by_principal_and_source_version_and_copied():
    instance = manager()
    class Source:
        cache_version = "v1"
        calls = 0
        def search(self, p, c):
            self.calls += 1
            return [candidate(c["organization_id"])]
    source = Source()
    register(instance, source.search, cache_ttl=60)
    async def run():
        first = await instance.call("search", {"query": "退款"}, {"organization_id": "a"})
        first.data[0]["content"] = "tampered"
        hit = await instance.call("search", {"query": "退款"}, {"organization_id": "a"})
        other = await instance.call("search", {"query": "退款"}, {"organization_id": "b"})
        source.cache_version = "v2"
        fresh = await instance.call("search", {"query": "退款"}, {"organization_id": "a"})
        return hit, other, fresh
    hit, other, fresh = asyncio.run(run())
    assert hit.cached and hit.data[0]["content"] != "tampered"
    assert not other.cached and other.data[0]["chunk_id"] == "b"
    assert not fresh.cached and source.calls == 3
