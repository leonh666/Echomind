"""Authenticated API integration: real transactions, fake retrieval/model."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from api import main, operations
from business.auth import AuthService
from business.store import BusinessStore, Principal
from evaluation.evaluator import EndToEndEvaluator, QualityScores
from mcp.tool_manager import Tool
from test_chat_api import configure as configure_chat
from test_knowledge_lifecycle import Collection, KnowledgeBase
from test_tool_manager_contracts import manager


PASSWORD = "test-password-1234"


@pytest.fixture
def service(tmp_path, monkeypatch):
    store = BusinessStore(tmp_path / "api.sqlite3")
    auth = AuthService(store)
    owner = auth.create_user("owner@example.invalid", PASSWORD, organization_name="Org A")
    customer = auth.create_user("customer@example.invalid", PASSWORD, organization_id=owner["organization_id"])
    other = auth.create_user("other@example.invalid", PASSWORD, organization_name="Org B")
    monkeypatch.setattr(operations, "_runtime", operations.Runtime(store=store, auth=auth))
    monkeypatch.setattr(operations, "_login_windows", {})
    monkeypatch.setenv("ECHOMIND_CORS_ORIGINS", "http://localhost:5173")
    client = TestClient(main.app)  # No lifespan: never creates provider/Redis/Chroma clients.
    def headers(email):
        token = auth.login(email + "@example.invalid", PASSWORD)["token"]
        return {"Authorization": "Bearer " + token}
    return client, store, owner, customer, other, headers


def test_cookie_login_logout_and_origin_validation(service):
    client, _, owner, _, _, _ = service
    assert client.get("/tasks").status_code == 401
    result = client.post("/auth/login", json={"email": "owner@example.invalid", "password": PASSWORD})
    assert result.status_code == 200 and "token" not in result.json()
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "SameSite=lax" in result.headers["set-cookie"]
    assert client.get("/auth/me").json()["organization_id"] == owner["organization_id"]
    body = {"payload": {"title": "登录问题", "description": "无法登录"}}
    assert client.post("/tasks", json=body, headers={"Origin": "https://untrusted.invalid"}).status_code == 403
    assert client.post("/tasks", json=body, headers={"Origin": "http://localhost:5173"}).status_code == 201
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/tasks").status_code == 401


def test_ticket_confirmation_rejects_old_revision_and_other_org(service):
    client, _, _, _, _, headers = service
    own, other = headers("customer"), headers("other")
    payload = {"title": "登录问题", "description": "无法登录"}
    draft = client.post("/tasks", json={"payload": payload, "idempotency_key": "create-a"}, headers=own).json()
    assert client.get("/tickets", headers=own).json()["items"] == []
    assert client.get(f"/tasks/{draft['id']}", headers=other).status_code == 404
    revised = client.put(f"/tasks/{draft['id']}/draft", json={"revision": 1, "payload": {**payload, "description": "已重置密码"}}, headers=own).json()
    confirm_body = lambda task: {key: task[key] for key in ("revision", "payload_hash", "idempotency_key")}
    assert client.post(f"/tasks/{draft['id']}/confirm", json=confirm_body(draft), headers=own).status_code == 409
    result = client.post(f"/tasks/{draft['id']}/confirm", json=confirm_body(revised), headers=own)
    assert result.status_code == 200
    replay = client.post(f"/tasks/{draft['id']}/confirm", json=confirm_body(revised), headers=own)
    assert replay.json()["replayed"]
    assert len(client.get("/tickets", headers=own).json()["items"]) == 1
    assert client.post("/knowledge/add", json={"documents": []}, headers=own).status_code == 403
    assert client.post("/eval/run", headers=own).status_code == 403


def test_feishu_confirmation_is_queued_without_network_and_scope_is_enforced(service):
    from integrations.feishu import FeishuConfig, FeishuService
    client, store, owner, _, _, headers = service
    config = FeishuConfig(True, owner['organization_id'], 'cli_test', 'secret-test', 'bas_test', 'tbl_test')
    store.feishu_target = config.target
    remote = SimpleNamespace(deliver=AsyncMock(return_value='rec_test'), check=AsyncMock(return_value={'ok': True}))
    integration = FeishuService(store, config, remote)
    operations.runtime().feishu = integration
    own = headers('customer')
    assert client.get('/integrations/feishu', headers=own).json()['enabled']
    assert not client.get('/integrations/feishu', headers=headers('other')).json()['enabled']
    draft = client.post('/tasks', json={'payload': {'title': '需要核查', 'description': '退款未到账'}}, headers=own).json()
    result = client.post(f"/tasks/{draft['id']}/confirm", json={key: draft[key] for key in ('revision', 'payload_hash', 'idempotency_key')}, headers=own).json()
    ticket = result['ticket']
    assert ticket['feishu_sync']['status'] == 'queued'
    remote.deliver.assert_not_called()
    assert client.get('/tickets', headers=own).json()['items'][0]['feishu_sync']['status'] == 'queued'
    assert client.post(f"/tickets/{ticket['id']}/feishu/retry", headers=own).status_code == 403
    assert client.post(f"/tickets/{ticket['id']}/feishu/retry", headers=headers('other')).status_code == 404
    assert client.post('/integrations/feishu/check', headers=own).status_code == 403
    assert client.post('/integrations/feishu/check', headers=headers('other')).status_code == 409
    assert client.post('/integrations/feishu/check', headers=headers('owner')).status_code == 200
    job = integration.deliveries.claim()
    integration.deliveries.finish(job, error_code='http_403')
    assert client.post(f"/tickets/{ticket['id']}/feishu/retry", headers=headers('owner')).json()['feishu_sync']['status'] == 'queued'
    assert 'secret-test' not in client.get('/tickets', headers=own).text


def test_knowledge_scope_cannot_be_forged_by_body_or_document_id(service, monkeypatch):
    client, _, owner, _, other, headers = service
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._collection = Collection()
    tool = Tool("knowledge_search", "fake", kb.search_handler, {})
    monkeypatch.setattr(main, "_tool_manager", SimpleNamespace(_tools={"knowledge_search": tool}))
    own_headers, other_headers = headers("owner"), headers("other")
    result = client.post("/knowledge/add", json={"documents": [{"title": "政策", "content": "七天退款", "metadata": {"organization_id": other["organization_id"]}}]}, headers=own_headers)
    assert result.status_code == 200
    own_docs = client.get("/knowledge/documents", headers=own_headers).json()["items"]
    assert own_docs[0]["organization_id"] == owner["organization_id"]
    identifier = own_docs[0]["document_id"]
    assert client.get("/knowledge/documents", headers=other_headers).json()["items"] == []
    assert client.get(f"/knowledge/documents/{identifier}", headers=other_headers).status_code == 404
    assert client.delete(f"/knowledge/documents/{identifier}", headers=other_headers).status_code == 404
    assert client.post("/knowledge/add", json={"documents": [{"document_id": identifier, "title": "覆写", "content": "恶意覆盖"}]}, headers=other_headers).status_code == 404
    assert client.post("/knowledge/upload", files={"file": ("bad.exe", b"content")}, headers=own_headers).status_code == 415
    assert client.post("/knowledge/upload", files={"file": ("bad.txt", b"\xff\xfe")}, headers=own_headers).status_code == 422


def test_readiness_detects_a_failed_chroma_dependency(service, monkeypatch):
    client, _, _, _, _, _ = service
    configure_chat(monkeypatch)
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._collection = Collection()
    kb._client = SimpleNamespace(heartbeat=lambda: 1)
    monkeypatch.setattr(main, "_tool_manager", SimpleNamespace(_tools={"knowledge_search": Tool("knowledge_search", "fake", kb.search_handler, {})}))
    assert client.get("/health/ready").status_code == 200
    def offline():
        raise ConnectionError("fake offline")
    kb._client.heartbeat = offline
    assert client.get("/health/ready").status_code == 503


def test_search_version_and_knowledge_base_filters_intersect_organization_scope(service, monkeypatch):
    client, _, owner, _, other, headers = service
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._collection = Collection()
    kb.add_documents([{"document_id": identifier, "title": "退款政策", "content": "退款测试规则",
                       "organization_id": org, "product_version": version, "knowledge_base_id": base}
                      for identifier, org, version, base in (
                          ("a-v1", owner["organization_id"], "v1", "default"),
                          ("a-v2", owner["organization_id"], "v2", "default"),
                          ("a-v2-other", owner["organization_id"], "v2", "another"),
                          ("b-v2", other["organization_id"], "v2", "default"))])
    tools = manager()
    tools.register(Tool("knowledge_search", "test", kb.search_handler, {}, supports_rerank=True))
    monkeypatch.setattr(main, "_tool_manager", tools)
    response = client.post("/search", params={"query": "退款", "product_version": "v2",
                                               "knowledge_base_id": "default", "strategy": "bm25", "rewrite": "false"},
                           headers=headers("owner"))
    assert response.status_code == 200
    assert [item["document_id"] for item in response.json()["results"]] == ["a-v2"]
    assert response.json()["filters"] == {"product_version": "v2", "knowledge_base_id": "default"}


def test_chat_uses_authenticated_identity_and_persists_only_a_draft(service, monkeypatch):
    client, store, _, customer, _, headers = service
    memory = configure_chat(monkeypatch)
    own = headers("customer")
    body = {"message": "请创建工单", "user_id": "forged", "conv_id": "confirmed-conversation", "client_request_id": "one"}
    response = client.post("/chat", json=body, headers=own)
    assert response.status_code == 200 and len(response.json()["tasks"]) == 1
    assert memory.saved[0][0] == customer["organization_id"] + ":" + customer["user"]["id"]
    assert client.get("/tickets", headers=own).json()["items"] == []
    assert client.post("/chat", json=body, headers=own).json()["tasks"][0]["id"] == response.json()["tasks"][0]["id"]
    assert client.post("/chat", json={"message": "继续", "conv_id": "confirmed-conversation"}, headers=headers("other")).status_code == 404
    no = client.post("/chat", json={"message": "不要创建工单"}, headers=own)
    assert no.json()["tasks"] == []


def test_eval_reports_baselines_and_actual_retrieval_scope_are_per_org(service, monkeypatch, tmp_path):
    client, _, owner, _, other, headers = service
    captured = []
    async def run(req):
        captured.append(req)
        req.evidence.append({"chunk_id": "c1", "content": "实际检索证据"})
        return SimpleNamespace(response="答复")
    judge = SimpleNamespace(_model="fake", judge=AsyncMock(return_value=QualityScores(.9, .9, .9, .9)))
    evaluator = EndToEndEvaluator(SimpleNamespace(run=run), SimpleNamespace(model="fake"), "fake",
                                  baseline_path=str(tmp_path / "baseline.json"), judge=judge)
    monkeypatch.setattr(main, "_evaluator", evaluator)
    body = {"intent_cases": [], "dialog_cases": [{"question": "退款问题"}], "dataset_version": "test-v1", "config_version": "native-v1"}
    own, outsider = headers("owner"), headers("other")
    first = client.post("/eval/run", json=body, headers=own)
    assert first.status_code == 200
    report = first.json()
    assert captured[0].principal is None  # Evaluations cannot mutate customer business state.
    assert captured[0].retrieval_context["organization_id"] == owner["organization_id"]
    assert judge.judge.call_args.kwargs["actual_evidence"][0]["chunk_id"] == "c1"
    assert client.post(f"/eval/reports/{report['run_id']}/baseline", headers=own).status_code == 200
    second = client.post("/eval/run", json=body, headers=own).json()
    assert second["metadata"]["baseline_comparison"]["comparable"]
    foreign = client.post("/eval/run", json=body, headers=outsider).json()
    assert foreign["metadata"]["baseline_comparison"]["status"] == "no_baseline"
    assert client.get(f"/eval/reports/{report['run_id']}", headers=outsider).status_code == 404
    visible = client.get("/eval/reports", headers=outsider).json()["items"]
    assert len(visible) == 1 and visible[0]["metadata"]["custom"]["organization_id"] == other["organization_id"]
