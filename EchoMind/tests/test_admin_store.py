"""Admin projections against real SQLite files; no services, model calls or secrets."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from business.admin import AdminService
from business.store import BusinessError, BusinessStore, Principal


METHODS = ("overview", "users", "activity", "conversations", "tasks", "tickets")
PAGED_METHODS = ("users", "activity", "conversations", "tasks", "tickets")
USER_FIELDS = {"id", "username", "email", "display_name", "memberships", "created_at"}
ACTIVITY_FIELDS = {"id", "organization_id", "actor_id", "actor_display_name", "actor_email",
                   "task_id", "ticket_id", "event_type", "details", "created_at"}
CONVERSATION_FIELDS = {"conversation_id", "organization_id", "owner_id", "created_at", "owner_email",
                       "owner_display_name", "request_count", "last_request_at"}


@pytest.fixture
def admin_data():
    # Keep disposable files in the repository, not the machine's protected temp.
    with TemporaryDirectory(prefix=".admin-test-", dir=Path(__file__).resolve().parent) as directory:
        clock = [1000.0]
        store = BusinessStore(Path(directory) / "admin.sqlite3", now=lambda: clock[0])
        people = (
            ("admin-a", "a", "admin"), ("alice", "a", "customer"), ("bob", "a", "customer"),
            ("support-a", "a", "support"), ("kb-a", "a", "kb_admin"),
            ("admin-b", "b", "admin"), ("other", "b", "customer"),
        )
        with store._transaction() as conn:
            for organization in ("a", "b", "empty"):
                conn.execute("INSERT INTO organizations VALUES(?,?,?)", (organization, "Workspace " + organization, clock[0]))
            for user_id, organization, role in people:
                conn.execute("INSERT INTO users(id,email,display_name,password_hash,password_salt,password_iterations,created_at) VALUES(?,?,?,?,?,?,?)",
                             (user_id, user_id + "@example.invalid", "Name " + user_id,
                              "PASSWORD_HASH_MUST_NOT_LEAK", "PASSWORD_SALT_MUST_NOT_LEAK", 600000, clock[0]))
                conn.execute("INSERT INTO memberships VALUES(?,?,?)", (user_id, organization, role))
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                         ("TOKEN_HASH_MUST_NOT_LEAK", "admin-a", "a", clock[0], 9999, None))
            conn.execute("INSERT INTO memberships VALUES(?,?,?)", ("admin-a", "empty", "admin"))

        actors = {uid: Principal(uid, org, role) for uid, org, role in people}
        payload = {"title": "测试问题", "description": "仅用于后台数据投影测试"}
        completed = store.create_draft(actors["alice"], payload)
        ticket = store.confirm_draft(actors["alice"], completed["id"], completed["revision"],
                                     completed["payload_hash"], completed["idempotency_key"])["ticket"]
        store.update_ticket_status(actors["admin-a"], ticket["id"], "in_progress", 1)
        store.create_draft(actors["bob"], {**payload, "title": "等待确认"})
        cancelled = store.create_draft(actors["bob"], {**payload, "title": "已取消草稿"})
        store.cancel_task(actors["bob"], cancelled["id"])
        foreign = store.create_draft(actors["other"], {**payload, "title": "另一组织的问题"})
        store.confirm_draft(actors["other"], foreign["id"], foreign["revision"], foreign["payload_hash"], foreign["idempotency_key"])

        store.claim_conversation(actors["alice"], "conv-a-alice")
        store.bind_request(actors["alice"], "request-a-1", "conv-a-alice")
        clock[0] += 1
        store.bind_request(actors["alice"], "request-a-2", "conv-a-alice")
        store.claim_conversation(actors["bob"], "conv-a-bob")  # No requests yet.
        store.claim_conversation(actors["admin-a"], "conv-a-admin")
        store.bind_request(actors["admin-a"], "request-a-admin", "conv-a-admin")
        store.claim_conversation(actors["other"], "conv-b-other")
        store.bind_request(actors["other"], "request-b-1", "conv-b-other")
        yield AdminService(store), store, actors, clock


def test_overview_counts_system_users_and_org_business_distributions(admin_data):
    admin, _, actors, _ = admin_data
    result = admin.overview(actors["admin-a"])
    assert result == {
        "organization_id": "a", "user_scope": "system",
        "counts": {"users": 7, "tasks": 3, "tickets": 1, "events": 7, "conversations": 3, "requests": 3},
        "task_status": {"cancelled": 1, "completed": 1, "waiting_confirmation": 1},
        "ticket_status": {"in_progress": 1},
    }
    other = admin.overview(actors["admin-b"])
    assert other["counts"] == {"users": 7, "tasks": 1, "tickets": 1, "events": 3, "conversations": 1, "requests": 1}
    assert other["task_status"] == {"completed": 1}
    assert other["ticket_status"] == {"open": 1}


def test_empty_business_organization_has_zero_counts_and_empty_statuses(admin_data):
    admin, _, _, _ = admin_data
    result = admin.overview(Principal("admin-a", "empty", "admin"))
    assert result["counts"] == {"users": 7, "tasks": 0, "tickets": 0, "events": 0, "conversations": 0, "requests": 0}
    assert result["task_status"] == {} and result["ticket_status"] == {}


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("user_id", ("alice", "bob", "support-a", "kb-a"))
def test_non_admins_cannot_access_any_backend_projection(admin_data, method, user_id):
    admin, _, actors, _ = admin_data
    with pytest.raises(BusinessError) as caught:
        getattr(admin, method)(actors[user_id])
    assert caught.value.http_status == 403


@pytest.mark.parametrize("method", METHODS)
def test_forged_admin_claim_is_rechecked_in_database(admin_data, method):
    admin, _, _, _ = admin_data
    with pytest.raises(BusinessError) as caught:
        getattr(admin, method)(Principal("alice", "a", "admin"))
    assert caught.value.http_status == 403


@pytest.mark.parametrize("method", METHODS)
def test_cached_principal_cannot_keep_access_after_demotion(admin_data, method):
    admin, store, actors, _ = admin_data
    store.assign_membership("admin-a", "a", "customer")
    with pytest.raises(BusinessError) as caught:
        getattr(admin, method)(actors["admin-a"])
    assert caught.value.http_status == 403
    with pytest.raises(BusinessError) as caught:
        getattr(admin, method)(Principal("admin-a", "a", "customer"))
    assert caught.value.http_status == 403


@pytest.mark.parametrize("method", METHODS)
def test_admin_cannot_claim_membership_in_another_organization(admin_data, method):
    admin, _, _, _ = admin_data
    with pytest.raises(BusinessError) as caught:
        getattr(admin, method)(Principal("admin-a", "b", "admin"))
    assert caught.value.http_status == 403


def test_users_lists_all_accounts_once_with_exact_safe_field_whitelist(admin_data):
    admin, _, actors, _ = admin_data
    result = admin.users(actors["admin-a"])
    assert result["total"] == 7 and result["limit"] == 50 and result["offset"] == 0
    assert {item["id"] for item in result["items"]} == {"admin-a", "alice", "bob", "support-a", "kb-a", "admin-b", "other"}
    assert {m["role"] for item in result["items"] for m in item["memberships"]} == {"admin", "customer", "support", "kb_admin"}
    assert all(set(item) == USER_FIELDS for item in result["items"])
    serialized = json.dumps(result)
    assert "PASSWORD_" not in serialized and "TOKEN_" not in serialized
    assert "admin-b@example.invalid" in serialized
    assert all(set(m) == {"organization_id", "organization_name", "role"} for item in result["items"] for m in item["memberships"])
    assert len(next(item for item in result["items"] if item["id"] == "admin-a")["memberships"]) == 2


def test_activity_joins_display_names_and_never_lists_other_org_events(admin_data):
    admin, _, actors, _ = admin_data
    page = admin.activity(actors["admin-a"])
    assert page["total"] == 7
    assert all(set(item) == ACTIVITY_FIELDS for item in page["items"])
    assert all(item["organization_id"] == "a" and item["actor_display_name"] == "Name " + item["actor_id"] for item in page["items"])
    assert {item["actor_id"] for item in page["items"]} == {"admin-a", "alice", "bob"}
    assert [item["id"] for item in page["items"]] == sorted((item["id"] for item in page["items"]), reverse=True)
    assert all(isinstance(item["details"], dict) for item in page["items"])


def test_conversations_include_owners_and_zero_request_counts_without_chat_content(admin_data):
    admin, _, actors, _ = admin_data
    page = admin.conversations(actors["admin-a"])
    assert page["total"] == 3
    by_id = {item["conversation_id"]: item for item in page["items"]}
    assert set(by_id) == {"conv-a-alice", "conv-a-bob", "conv-a-admin"}
    assert all(set(item) == CONVERSATION_FIELDS for item in page["items"])
    assert by_id["conv-a-alice"]["request_count"] == 2
    assert by_id["conv-a-alice"]["last_request_at"] == 1001.0
    assert by_id["conv-a-alice"]["owner_email"] == "alice@example.invalid"
    assert by_id["conv-a-bob"]["request_count"] == 0
    assert by_id["conv-a-bob"]["last_request_at"] is None
    assert all(item["owner_display_name"] == "Name " + item["owner_id"] for item in page["items"])
    assert "content" not in json.dumps(page)


@pytest.mark.parametrize("method", PAGED_METHODS)
def test_pages_are_stable_and_total_is_unpaginated(admin_data, method):
    admin, _, actors, _ = admin_data
    listing = getattr(admin, method)
    full = listing(actors["admin-a"], limit=100)
    first = listing(actors["admin-a"], limit=2, offset=0)
    second = listing(actors["admin-a"], limit=2, offset=2)
    assert first == {"items": full["items"][:2], "total": full["total"], "limit": 2, "offset": 0}
    assert second == {"items": full["items"][2:4], "total": full["total"], "limit": 2, "offset": 2}
    assert listing(actors["admin-a"], limit=1, offset=100)["items"] == []


@pytest.mark.parametrize("method", PAGED_METHODS)
@pytest.mark.parametrize("pagination", ({"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1.5},
                                         {"offset": -1}, {"offset": False}, {"offset": "0"}, {"offset": 2**63}))
def test_invalid_pagination_is_rejected(admin_data, method, pagination):
    admin, _, actors, _ = admin_data
    with pytest.raises(BusinessError) as caught:
        getattr(admin, method)(actors["admin-a"], **pagination)
    assert caught.value.http_status == 400


def test_admin_reads_do_not_write_events_expire_tasks_or_change_database(admin_data):
    admin, store, actors, clock = admin_data
    with store._transaction() as conn:
        before = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                  for table in ("users", "memberships", "sessions", "tasks", "tickets", "events", "approvals", "operations",
                                "conversation_ownership", "request_ownership")}
    clock[0] = 99999.0  # The pending task has elapsed, but admin reads are read-only.
    for method in METHODS:
        json.dumps(getattr(admin, method)(actors["admin-a"]), ensure_ascii=False)
    with store._transaction() as conn:
        after = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                 for table in before}
    assert after == before
