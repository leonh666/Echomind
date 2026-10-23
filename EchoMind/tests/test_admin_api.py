"""Role boundaries through HTTP, using real SQLite sessions and no live services."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import main, operations
from business.auth import AuthService
from business.store import BusinessStore, Principal


PASSWORD = "test-password-1234"
ADMIN_LISTS = ("users", "activity", "conversations", "tasks", "tickets")
BACKEND_ROUTES = (
    ("GET", "/admin/overview"), ("GET", "/admin/users"),
    ("GET", "/admin/activity"), ("GET", "/admin/conversations"),
    ("GET", "/admin/tasks"), ("GET", "/admin/tickets"),
    ("GET", "/monitor"), ("GET", "/metrics"),
    ("GET", "/trace/tools"), ("GET", "/trace/tool/request-a"),
    ("GET", "/eval/reports"), ("POST", "/eval/run"),
    ("GET", "/skills"), ("POST", "/skills/reload"),
    ("GET", "/knowledge/stats"), ("GET", "/knowledge/documents"),
    ("GET", "/knowledge/documents/some-document"),
    ("POST", "/knowledge/add"), ("POST", "/search?query=test"),
    ("GET", "/docs"), ("GET", "/openapi.json"), ("GET", "/redoc"),
)


@pytest.fixture
def admin_api(tmp_path, monkeypatch):
    store = BusinessStore(tmp_path / "admin-http.sqlite3")
    auth = AuthService(store)
    # Use a smaller derivation count only in this disposable test fixture. The
    # production create-user policy is tested separately in business-store tests.
    people = (("admin-a", "a", "admin"), ("alice", "a", "customer"),
              ("bob", "a", "customer"), ("support", "a", "support"),
              ("kb", "a", "kb_admin"), ("admin-b", "b", "admin"),
              ("foreign", "b", "customer"))
    salt = "12" * 32
    password_hash = auth._password(PASSWORD, salt, 1000)
    with store._transaction() as conn:
        for org in ("a", "b"):
            conn.execute("INSERT INTO organizations VALUES(?,?,?)", (org, "Organization " + org, 1000))
        for user_id, org, role in people:
            conn.execute("INSERT INTO users(id,email,display_name,password_hash,password_salt,password_iterations,created_at) VALUES(?,?,?,?,?,?,?)",
                         (user_id, user_id + "@example.invalid", "Display " + user_id,
                          password_hash, salt, 1000, 1000))
            conn.execute("INSERT INTO memberships VALUES(?,?,?)", (user_id, org, role))
    from business.auth import migrate_usernames
    with store._transaction() as conn:
        migrate_usernames(conn)
    actors = {uid: Principal(uid, org, role) for uid, org, role in people}
    tokens = {uid: auth.login(uid + "@example.invalid", PASSWORD)["token"] for uid, _, _ in people}
    headers = {uid: {"Authorization": "Bearer " + token} for uid, token in tokens.items()}
    drafts, tickets = {}, {}
    for uid in ("alice", "bob", "foreign"):
        draft = store.create_draft(actors[uid], {"title": "Issue " + uid, "description": "A test support request"})
        result = store.confirm_draft(actors[uid], draft["id"], draft["revision"],
                                     draft["payload_hash"], draft["idempotency_key"])
        drafts[uid], tickets[uid] = result["task"], result["ticket"]
        store.claim_conversation(actors[uid], "conversation-" + uid)
        store.bind_request(actors[uid], "request-" + uid, "conversation-" + uid)
    monkeypatch.setattr(operations, "_runtime", operations.Runtime(store, auth))
    monkeypatch.setattr(operations, "_login_windows", {})
    monkeypatch.delenv("ECHOMIND_METRICS_TOKEN", raising=False)
    monkeypatch.delenv("ECHOMIND_METRICS_TOKEN_FILE", raising=False)
    monkeypatch.setattr(main, "_orchestrator", SimpleNamespace(get_stats=lambda: {"private-agent": {"total": 777}}))
    monkeypatch.setattr(main, "_skill_manager", SimpleNamespace(summary=lambda: {"count": 0, "skills": [], "errors": []}))
    monkeypatch.setattr(main, "_monitor", SimpleNamespace(summary=lambda: {"agent_stats": {}, "active_alerts": []}))
    return SimpleNamespace(client=TestClient(main.app), store=store, auth=auth,
                           headers=headers, actors=actors, drafts=drafts, tickets=tickets)


@pytest.mark.parametrize("method,path", BACKEND_ROUTES)
def test_customer_cannot_read_backend_even_with_forged_role(admin_api, method, path):
    headers = {**admin_api.headers["alice"], "X-Role": "admin", "X-Organization-ID": "b"}
    response = admin_api.client.request(method, path, headers=headers)
    assert response.status_code == 403, (method, path, response.text)


@pytest.mark.parametrize("method,path", BACKEND_ROUTES)
def test_backend_requires_login_even_when_chat_auth_is_disabled(admin_api, method, path):
    operations._runtime.auth_required = False
    response = admin_api.client.request(method, path)
    assert response.status_code == 401, (method, path, response.text)


@pytest.mark.parametrize("path", ("/admin/users/", "/monitor/", "/knowledge/stats/", "/trace/tools/", "/metrics/"))
def test_trailing_slash_does_not_bypass_role_checks(admin_api, path):
    response = admin_api.client.get(path, headers=admin_api.headers["alice"], follow_redirects=False)
    assert response.status_code == 403


def test_me_is_current_user_profile_with_a_safe_field_allowlist(admin_api):
    response = admin_api.client.get("/auth/me?user_id=bob&organization_id=b", headers=admin_api.headers["alice"])
    assert response.status_code == 200
    profile = response.json()
    assert profile["authenticated"] and profile["user_id"] == "alice"
    assert profile["organization_id"] == "a" and profile["role"] == "customer"
    assert profile["user"] == {"id": "alice", "email": "alice@example.invalid",
                               "display_name": "Display alice", "username": "alice", "created_at": 1000.0}
    assert profile["organization"] == {"id": "a", "name": "Organization a", "created_at": 1000.0}
    serialized = json.dumps(profile)
    assert all(secret not in serialized for secret in ("password", "salt", "token", "sessions"))


def test_cookie_login_refresh_profile_and_logout(admin_api):
    client = admin_api.client
    result = client.post("/auth/login", json={"email": "alice@example.invalid", "password": PASSWORD})
    assert result.status_code == 200 and "token" not in result.json()
    assert "HttpOnly" in result.headers["set-cookie"]
    assert client.get("/auth/me").json()["user"]["email"] == "alice@example.invalid"
    assert client.get("/admin/overview").status_code == 403
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/auth/me").status_code == 401


def test_customer_sees_only_own_tasks_tickets_and_no_audit_history(admin_api):
    client, headers = admin_api.client, admin_api.headers["alice"]
    assert [item["id"] for item in client.get("/tasks", headers=headers).json()["items"]] == [admin_api.drafts["alice"]["id"]]
    assert [item["id"] for item in client.get("/tickets", headers=headers).json()["items"]] == [admin_api.tickets["alice"]["id"]]
    for uid in ("bob", "foreign"):
        assert client.get("/tasks/" + admin_api.drafts[uid]["id"], headers=headers).status_code == 404
        assert client.get("/tickets/" + admin_api.tickets[uid]["id"], headers=headers).status_code == 404
    for collection, record in (("tasks", admin_api.drafts["alice"]), ("tickets", admin_api.tickets["alice"])):
        path = f"/{collection}/{record['id']}/events"
        assert client.get(path, headers=headers).status_code == 403
        assert client.get(path, headers=admin_api.headers["admin-a"]).status_code == 200
        assert client.get(path, headers=admin_api.headers["support"]).status_code == 200


@pytest.mark.parametrize("collection", ADMIN_LISTS)
def test_admin_lists_paginate_all_own_org_data_and_exclude_foreign_org(admin_api, collection):
    client, headers = admin_api.client, admin_api.headers["admin-a"]
    full_response = client.get("/admin/" + collection, params={"limit": 100}, headers=headers)
    assert full_response.status_code == 200, full_response.text
    full = full_response.json()
    assert full["total"] == len(full["items"]) and full["total"] >= 2
    first = client.get("/admin/" + collection, params={"limit": 1, "offset": 0}, headers=headers).json()
    second = client.get("/admin/" + collection, params={"limit": 1, "offset": 1}, headers=headers).json()
    assert first == {"items": full["items"][:1], "total": full["total"], "limit": 1, "offset": 0}
    assert second == {"items": full["items"][1:2], "total": full["total"], "limit": 1, "offset": 1}
    serialized = json.dumps(full)
    assert "foreign" not in serialized and "admin-b" not in serialized
    if collection == "users":
        assert full["total"] == 5
        assert all(set(item) == {"id", "email", "display_name", "role", "created_at"} for item in full["items"])
    if collection in ("tasks", "tickets"):
        assert {item["created_by"] for item in full["items"]} == {"alice", "bob"}


def test_overview_counts_the_whole_current_organization_only(admin_api):
    response = admin_api.client.get("/admin/overview?organization_id=b", headers=admin_api.headers["admin-a"])
    assert response.status_code == 200
    data = response.json()
    assert data["organization_id"] == "a"
    assert data["counts"] == {"users": 5, "tasks": 2, "tickets": 2, "events": 6, "conversations": 2, "requests": 2}
    assert data["task_status"] == {"completed": 2}
    foreign = admin_api.client.get("/admin/overview", headers=admin_api.headers["admin-b"]).json()
    assert foreign["organization_id"] == "b" and foreign["counts"]["tickets"] == 1


@pytest.mark.parametrize("collection", ADMIN_LISTS)
@pytest.mark.parametrize("pagination", ({"limit": 0}, {"limit": 101}, {"offset": -1}))
def test_admin_pagination_rejects_invalid_bounds(admin_api, collection, pagination):
    response = admin_api.client.get("/admin/" + collection, params=pagination, headers=admin_api.headers["admin-a"])
    assert response.status_code in {400, 422}


def test_existing_session_loses_backend_access_after_demotion(admin_api):
    headers = admin_api.headers["admin-a"]
    assert admin_api.client.get("/admin/overview", headers=headers).status_code == 200
    admin_api.store.assign_membership("admin-a", "a", "customer")
    assert admin_api.client.get("/admin/overview", headers=headers).status_code == 403
    assert admin_api.client.get("/monitor", headers=headers).status_code == 403
    assert admin_api.client.get("/auth/me", headers=headers).json()["role"] == "customer"


@pytest.mark.parametrize("uid", ("support", "kb"))
def test_operational_roles_have_no_full_admin_dashboard(admin_api, uid):
    assert admin_api.client.get("/admin/users", headers=admin_api.headers[uid]).status_code == 403


def test_kb_admin_can_read_knowledge_capabilities_but_not_runtime_data(admin_api):
    assert admin_api.client.get("/skills", headers=admin_api.headers["kb"]).status_code == 200
    assert admin_api.client.post("/skills/reload", headers=admin_api.headers["kb"]).status_code == 403
    assert admin_api.client.get("/monitor", headers=admin_api.headers["kb"]).status_code == 403


def test_health_does_not_publish_backend_statistics(admin_api):
    response = admin_api.client.get("/health")
    assert response.status_code == 200 and response.json()["status"] == "ok"
    assert "agents" not in response.json() and "777" not in response.text


def test_metrics_scraper_credential_is_limited_to_metrics(admin_api, monkeypatch):
    monkeypatch.setenv("ECHOMIND_METRICS_TOKEN", "synthetic-metrics-token-used-only-in-tests")
    headers = {"Authorization": "Bearer synthetic-metrics-token-used-only-in-tests"}
    response = admin_api.client.get("/metrics", headers=headers)
    assert response.status_code == 200 and "text/plain" in response.headers["content-type"]
    assert admin_api.client.get("/admin/users", headers=headers).status_code == 401
    assert admin_api.client.get("/monitor", headers=headers).status_code == 401
    assert admin_api.client.get("/metrics", headers={"Authorization": "Bearer wrong-token"}).status_code == 401
    assert admin_api.client.get("/metrics", headers=admin_api.headers["alice"]).status_code == 403
    assert admin_api.client.get("/metrics", headers=admin_api.headers["admin-a"]).status_code == 200
    assert admin_api.client.get("/metrics").status_code == 401


def test_management_policy_also_applies_when_app_is_mounted_below_a_prefix(admin_api):
    from fastapi import FastAPI
    outer = FastAPI()
    outer.mount("/api/python", main.app)
    client = TestClient(outer)
    assert client.get("/api/python/monitor", headers=admin_api.headers["alice"]).status_code == 403
    assert client.get("/api/python/admin/users", headers=admin_api.headers["admin-a"]).status_code == 200


def test_admin_task_projection_does_not_expose_metadata_or_confirmation_capabilities(admin_api):
    task = admin_api.store.create_draft(admin_api.actors["alice"], {
        "title": "Safe title", "description": "Safe description", "metadata": {"secret": "synthetic-private-metadata"}
    })
    response = admin_api.client.get("/admin/tasks", headers=admin_api.headers["admin-a"])
    item = next(item for item in response.json()["items"] if item["id"] == task["id"])
    assert item["expires_at"] == item["confirmation_expires_at"] == task["expires_at"]
    assert set(item["payload"]) == {"title", "description", "category", "priority"}
    assert all(field not in item for field in ("idempotency_key", "payload_hash", "draft"))
    assert "synthetic-private-metadata" not in response.text


def test_metrics_token_file_has_priority_and_missing_file_fails_closed(admin_api, monkeypatch, tmp_path):
    token_file = tmp_path / "metrics-token"
    token_file.write_text("synthetic-file-metrics-token\n", encoding="utf-8")
    monkeypatch.setenv("ECHOMIND_METRICS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("ECHOMIND_METRICS_TOKEN", "synthetic-env-metrics-token")
    file_headers = {"Authorization": "Bearer synthetic-file-metrics-token"}
    assert admin_api.client.get("/metrics", headers=file_headers).status_code == 200
    assert admin_api.client.get("/admin/users", headers=file_headers).status_code == 401
    assert admin_api.client.get("/metrics", headers={"Authorization": "Bearer synthetic-env-metrics-token"}).status_code == 401
    monkeypatch.setenv("ECHOMIND_METRICS_TOKEN_FILE", str(tmp_path / "absent-token"))
    assert admin_api.client.get("/metrics", headers=file_headers).status_code == 401
