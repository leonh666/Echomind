"""Business invariants against a real SQLite file, without model/service calls."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from business.auth import AuthService
from business.maintenance import inspect
from business.store import BusinessError, BusinessStore, Principal


PAYLOAD = {"title": "登录失败", "description": "重置密码后仍无法登录", "category": "technical"}


@pytest.fixture
def business(tmp_path):
    clock = [1000.0]
    store = BusinessStore(tmp_path / "business.sqlite3", confirmation_ttl_seconds=60, now=lambda: clock[0])
    with store._transaction() as db:
        for org in ("a", "b"):
            db.execute("INSERT INTO organizations VALUES(?,?,?)", (org, org, clock[0]))
        for uid, org, role in (("alice", "a", "customer"), ("bob", "a", "customer"),
                               ("support", "a", "support"), ("other", "b", "admin")):
            db.execute("INSERT INTO users(id,email,display_name,password_hash,password_salt,password_iterations,created_at) VALUES(?,?,?,?,?,?,?)", (uid, uid + "@example.invalid", uid, "hash", "00", 1, clock[0]))
            db.execute("INSERT INTO memberships VALUES(?,?,?)", (uid, org, role))
    return store, clock, Principal("alice", "a", "customer")


def confirm(store, actor, draft, key=None):
    return store.confirm_draft(actor, draft["id"], draft["revision"], draft["payload_hash"], key or draft["idempotency_key"])


def test_draft_survives_restart_and_confirmation_replays(business):
    store, clock, actor = business
    draft = store.create_draft(actor, PAYLOAD, "create-1")
    assert store.list_tickets(actor) == []
    restarted = BusinessStore(store.db_path, now=lambda: clock[0])
    assert restarted.get_task(actor, draft["id"])["payload"]["title"] == PAYLOAD["title"]
    result = confirm(restarted, actor, draft)
    assert result["task"]["status"] == "completed"
    assert confirm(restarted, actor, draft)["ticket"]["id"] == result["ticket"]["id"]
    assert confirm(restarted, actor, draft, "another-confirm-key")["replayed"]
    assert len(restarted.list_tickets(actor)) == 1
    assert store.create_draft(actor, PAYLOAD, "create-1")["status"] == "completed"


def test_concurrent_confirmation_creates_one_ticket_and_one_audit(business):
    store, _, actor = business
    draft = store.create_draft(actor, PAYLOAD)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: confirm(store, actor, draft), range(12)))
    assert len({r["ticket"]["id"] for r in results}) == 1
    assert sum(not r["replayed"] for r in results) == 1
    assert sum(e["event_type"] == "ticket.created" for e in store.list_events(actor)) == 1


def test_revised_payload_invalidates_old_confirmation(business):
    store, _, actor = business
    old = store.create_draft(actor, PAYLOAD)
    new = store.revise_draft(actor, old["id"], {**PAYLOAD, "description": "补充错误代码 E101"}, old["revision"])
    assert new["revision"] == 2 and new["payload_hash"] != old["payload_hash"]
    with pytest.raises(BusinessError, match="current draft"):
        confirm(store, actor, old)
    assert store.list_tickets(actor) == []
    assert confirm(store, actor, new)["ticket"]["description"] == "补充错误代码 E101"


@pytest.mark.parametrize("finish", ["cancel_task", "reject_task"])
def test_cancelled_or_rejected_draft_cannot_create_ticket(business, finish):
    store, _, actor = business
    draft = store.create_draft(actor, PAYLOAD)
    getattr(store, finish)(actor, draft["id"], draft["revision"])
    with pytest.raises(BusinessError):
        confirm(store, actor, draft)
    assert not store.list_tickets(actor)


def test_expiration_is_committed_even_on_confirmation_error(business):
    store, clock, actor = business
    draft = store.create_draft(actor, PAYLOAD)
    clock[0] += 60
    with pytest.raises(BusinessError):
        confirm(store, actor, draft)
    assert store.get_task(actor, draft["id"])["status"] == "expired"
    assert sum(e["event_type"] == "task.expired" for e in store.list_events(actor)) == 1


def test_idempotency_key_rejects_changed_body_and_different_actor(business):
    store, _, actor = business
    store.create_draft(actor, PAYLOAD, "same-key")
    for candidate, payload in ((actor, {**PAYLOAD, "title": "另一个问题"}), (Principal("bob", "a", "customer"), PAYLOAD)):
        with pytest.raises(BusinessError) as caught:
            store.create_draft(candidate, payload, "same-key")
        assert caught.value.code == "idempotency_conflict"


@pytest.mark.parametrize("intruder", [Principal("bob", "a", "customer"), Principal("other", "b", "admin")])
def test_task_ticket_and_events_are_isolated(business, intruder):
    store, _, actor = business
    draft = store.create_draft(actor, PAYLOAD)
    ticket = confirm(store, actor, draft)["ticket"]
    for action in (lambda: store.get_task(intruder, draft["id"]), lambda: store.get_ticket(intruder, ticket["id"]),
                   lambda: store.list_events(intruder, task_id=draft["id"]), lambda: confirm(store, intruder, draft)):
        with pytest.raises(BusinessError) as caught:
            action()
        assert caught.value.http_status == 404
    assert store.list_tasks(intruder) == []


def test_forged_or_stale_role_is_rejected(business):
    store, _, actor = business
    with pytest.raises(BusinessError):
        store.list_tasks(Principal(actor.user_id, actor.organization_id, "admin"))
    store.assign_membership(actor.user_id, actor.organization_id, "support")
    with pytest.raises(BusinessError):
        store.list_tasks(actor)


def test_ticket_state_machine_checks_role_and_version(business):
    store, _, actor = business
    ticket = confirm(store, actor, store.create_draft(actor, PAYLOAD))["ticket"]
    support = Principal("support", "a", "support")
    with pytest.raises(BusinessError):
        store.update_ticket_status(actor, ticket["id"], "in_progress", 1)
    with pytest.raises(BusinessError):
        store.update_ticket_status(support, ticket["id"], "closed", 1)
    assert store.update_ticket_status(support, ticket["id"], "in_progress", 1)["version"] == 2
    with pytest.raises(BusinessError) as caught:
        store.update_ticket_status(support, ticket["id"], "resolved", 1)
    assert caught.value.code == "version_conflict"
    assert store.update_ticket_status(support, ticket["id"], "resolved", 2)["status"] == "resolved"


def test_request_ownership_blocks_other_customers_and_organizations(business):
    store, _, actor = business
    store.claim_conversation(actor, "conv-a")
    store.bind_request(actor, "req-a", "conv-a")
    assert store.can_read_request(actor, "req-a")
    for intruder in (Principal("bob", "a", "customer"), Principal("other", "b", "admin")):
        assert not store.can_read_request(intruder, "req-a")
        with pytest.raises(BusinessError):
            store.claim_conversation(intruder, "conv-a")


def test_online_backup_can_restore_pending_draft(business, tmp_path):
    store, _, actor = business
    draft = store.create_draft(actor, PAYLOAD)
    path = tmp_path / "backup.sqlite3"
    assert store.backup(path)["integrity_check"] == "ok"
    assert inspect(path)["counts"]["tasks"] == 1
    restored = BusinessStore(path, now=lambda: 1001)
    assert confirm(restored, actor, restored.get_task(actor, draft["id"]))["ticket"]["title"] == PAYLOAD["title"]
    with pytest.raises(BusinessError):
        store.backup(path)


def test_failed_transaction_leaves_no_ticket_or_consumed_approval(business, monkeypatch):
    store, _, actor = business
    draft = store.create_draft(actor, PAYLOAD)
    original = store._event
    def fail(db, who, event_type, **kwargs):
        if event_type == "ticket.created":
            raise RuntimeError("simulated crash before commit")
        return original(db, who, event_type, **kwargs)
    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError):
        confirm(store, actor, draft)
    assert store.get_task(actor, draft["id"])["status"] == "waiting_confirmation"
    assert not store.list_tickets(actor)
    monkeypatch.setattr(store, "_event", original)
    assert confirm(store, actor, draft)["ticket"]


def test_auth_sessions_are_hashed_revocable_expiring_and_revalidate_role(tmp_path):
    clock = [1000]
    store = BusinessStore(tmp_path / "auth.sqlite3", now=lambda: clock[0])
    auth = AuthService(store, session_ttl_seconds=60)
    user = auth.create_user("admin@example.invalid", "test-password-1234", organization_name="Demo")
    session = auth.login("ADMIN@example.invalid", "test-password-1234")
    actor = auth.authenticate(session["token"])
    with store._connect() as db:
        row = dict(db.execute("SELECT * FROM sessions").fetchone())
        assert session["token"] not in json.dumps(row)
        assert db.execute("SELECT password_hash FROM users").fetchone()[0] != "test-password-1234"
    store.assign_membership(actor.user_id, actor.organization_id, "customer")
    assert auth.authenticate(session["token"]).role == "customer"
    auth.logout(session["token"])
    with pytest.raises(BusinessError):
        auth.authenticate(session["token"])
    new = auth.login("admin@example.invalid", "test-password-1234")
    clock[0] += 60
    with pytest.raises(BusinessError):
        auth.authenticate(new["token"])
    with pytest.raises(BusinessError):
        auth.login("admin@example.invalid", "incorrect")
