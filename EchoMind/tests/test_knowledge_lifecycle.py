"""Metadata and publication contracts using an in-memory Chroma adapter."""
import copy
import sys
import types

import pytest

try:
    import chromadb
except ModuleNotFoundError:
    sys.modules["chromadb"] = types.ModuleType("chromadb")

from mcp.knowledge_base import KnowledgeBase


def matches(meta, where):
    if not where:
        return True
    if "$and" in where:
        return all(matches(meta, part) for part in where["$and"])
    if "$or" in where:
        return any(matches(meta, part) for part in where["$or"])
    return all(meta.get(key) == value for key, value in where.items())


class Collection:
    metadata = {"hnsw:space": "l2"}

    def __init__(self):
        self.rows = {}
        self.fail_publish = False
        self.fail_delete = False
        self.calls = []

    def get(self, where=None, **kwargs):
        rows = [(key, *value) for key, value in self.rows.items() if matches(value[1], where)]
        self.calls.append(("get", copy.deepcopy(where)))
        return {"ids": [r[0] for r in rows], "documents": [r[1] for r in rows], "metadatas": [copy.deepcopy(r[2]) for r in rows]}

    def upsert(self, ids, documents, metadatas):
        if self.fail_publish and any(m.get("_publication") == "published" for m in metadatas):
            self.fail_publish = False
            raise RuntimeError("publish failed")
        for key, text, meta in zip(ids, documents, metadatas):
            self.rows[key] = (text, copy.deepcopy(meta))

    def update(self, ids, metadatas):
        for key, meta in zip(ids, metadatas):
            self.rows[key] = (self.rows[key][0], copy.deepcopy(meta))

    def delete(self, ids):
        if self.fail_delete:
            raise RuntimeError("delete failed")
        for key in ids:
            self.rows.pop(key, None)

    def query(self, query_texts, n_results, where=None, **kwargs):
        rows = self.get(where)
        self.calls.append(("query", copy.deepcopy(where)))
        # Deliberately retain insertion order: vector ranking itself is not tested.
        return {"ids": [rows["ids"][:n_results]], "distances": [[2.5 + i for i in range(min(n_results, len(rows["ids"])))] ]}


@pytest.fixture
def kb():
    result = KnowledgeBase.__new__(KnowledgeBase)
    result._collection = Collection()
    return result


def document(content="退款应在七天内申请。", org="a", **extra):
    return {"title": "退款政策", "content": content, "organization_id": org, "source_url": "https://example.invalid/refunds", **extra}


def test_update_removes_old_chunks_preserves_stable_identity_and_invalidates_cache(kb):
    kb.add_documents([document("旧规定" * 400)])
    old = kb.list_documents()[0]
    old_ids = {c["chunk_id"] for c in old["chunks"]}
    version = kb.cache_version
    kb.update_document(old["document_id"], {"content": "新规定：十天。"})
    new = kb.list_documents()[0]
    assert new["document_id"] == old["document_id"]
    assert new["document_version"] != old["document_version"]
    assert new["chunk_count"] == 1
    assert not old_ids & set(kb._collection.rows)
    assert kb.cache_version != version
    assert kb.delete_document(new["document_id"]) == 1
    assert kb.search("退款", where={"organization_id": "a"}) == []


@pytest.mark.parametrize("strategy", ["vector", "bm25", "hybrid"])
def test_every_retriever_applies_scope_before_recall(kb, strategy):
    kb.add_documents([document(org="a"), document("退款机密规则。", org="b")])
    results = kb.search("退款", strategy=strategy, where={"organization_id": "a"})
    assert results and all(r["organization_id"] == "a" for r in results)
    assert all(where == {"organization_id": "a"} for _, where in kb._collection.calls if _ in {"get", "query"} and where and "document_id" not in where)
    assert results[0]["chunk_id"] and results[0]["document_version"] and results[0]["source_url"]
    if strategy == "vector":
        assert results[0]["score"] == -2.5
        assert results[0]["score_type"] == "negative_distance"


def test_tool_supplied_filter_cannot_expand_authenticated_scope(kb):
    import asyncio
    kb.add_documents([document(org="a"), document(org="b")])
    found = asyncio.run(kb.search_handler({"query": "退款", "where": {"organization_id": "b"}}, {"where": {"organization_id": "a"}}))
    assert found == []


def test_failed_publish_restores_old_document(kb):
    kb.add_documents([document("旧退款规定。")])
    old = kb.list_documents()[0]
    before = copy.deepcopy(kb._collection.rows)
    kb._collection.fail_publish = True
    with pytest.raises(RuntimeError, match="rollback completed"):
        kb.update_document(old["document_id"], {"content": "新退款规定。"})
    assert kb._collection.rows == before
    assert kb.search("退款", strategy="bm25")[0]["content"] == "旧退款规定。"


def test_incomplete_rollback_and_crash_staging_are_hidden(kb):
    kb.add_documents([document()])
    old = kb.list_documents()[0]
    kb._collection.fail_delete = True
    with pytest.raises(RuntimeError, match="rollback incomplete"):
        kb.update_document(old["document_id"], {"content": "新版本"})
    assert kb.search("退款", where={"organization_id": "a"}) == []
    assert kb.stats(where={"organization_id": "a"})["quarantined_documents"] == [old["document_id"]]
    assert kb.stats(where={"organization_id": "b"})["quarantined_documents"] == []
    # A fresh instance discovers leftover staging after process restart.
    for key, (text, meta) in kb._collection.rows.items():
        meta["_publication"] = "staged"
    fresh = KnowledgeBase.__new__(KnowledgeBase)
    fresh._collection = kb._collection
    assert fresh.search("退款", strategy="bm25") == []


def test_legacy_migration_updates_only_metadata(kb):
    kb._collection.rows["old-id"] = ("退款七天。", {"title": "旧政策", "chunk_index": 0})
    assert kb.search("退款", where={"organization_id": "public"}) == []
    assert kb.migrate_legacy_documents() == 1
    result = kb.search("退款", strategy="bm25", where={"organization_id": "public"})
    assert result[0]["chunk_id"] == "old-id"
    assert result[0]["content"] == "退款七天。"
    assert kb.migrate_legacy_documents() == 0


def test_long_sentence_is_bounded_and_reconstructs_exact_text(kb):
    content = ("错误 E102 的说明。\n" * 200) + "长" * 1800
    chunks = kb._chunk_text(content)
    assert all(0 < len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunks) == content


def test_content_only_update_preserves_custom_metadata(kb):
    kb.add_documents([document(metadata={"source_license": "test-only"})])
    identifier = kb.list_documents()[0]["document_id"]
    kb.update_document(identifier, {"content": "更新后的退款政策。"})
    assert kb.get_document(identifier)["metadata"]["source_license"] == "test-only"


def test_disabled_local_fallback_never_opens_another_database(monkeypatch):
    module = sys.modules["chromadb"]
    def offline(**kwargs):
        raise ConnectionError("test service offline")
    def unwanted(**kwargs):
        pytest.fail("local fallback must not run")
    monkeypatch.setattr(module, "Settings", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(module, "HttpClient", offline, raising=False)
    monkeypatch.setattr(module, "PersistentClient", unwanted, raising=False)
    with pytest.raises(RuntimeError, match="fallback is disabled"):
        KnowledgeBase(allow_local_fallback=False)


def test_maintenance_constructor_does_not_seed_an_empty_collection(monkeypatch):
    module = sys.modules["chromadb"]
    class Empty:
        def count(self):
            pytest.fail("maintenance must not check whether to seed")
    client = types.SimpleNamespace(heartbeat=lambda: 1, get_or_create_collection=lambda **kwargs: Empty())
    monkeypatch.setattr(module, "Settings", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(module, "HttpClient", lambda **kwargs: client, raising=False)
    KnowledgeBase(allow_local_fallback=False, seed_defaults=False)


def test_document_id_cannot_overwrite_other_organization(kb):
    kb.add_documents([document()])
    identifier = kb.list_documents()[0]["document_id"]
    with pytest.raises(ValueError):
        kb.add_documents([document(org="b", document_id=identifier)])
    assert kb.get_document(identifier)["organization_id"] == "a"


@pytest.mark.parametrize("top_k", [0, -1, True, 101, 2.2])
def test_invalid_top_k_is_rejected(kb, top_k):
    with pytest.raises(ValueError):
        kb.search("问题", top_k)
