"""SQLite business transactions; no framework, model calls, or global database path.

Each public operation opens its own connection. Approval consumption, ticket creation,
task completion, audit and idempotency results commit in one BEGIN IMMEDIATE transaction.
Principal instances must come from AuthService; membership is still rechecked here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


ROLES = frozenset({"customer", "support", "kb_admin", "admin"})
MANAGERS = frozenset({"support", "admin"})
TRANSITIONS = {
    "open": {"in_progress", "cancelled"},
    "in_progress": {"resolved", "cancelled"},
    "resolved": {"closed", "in_progress"},
    "closed": set(),
    "cancelled": set(),
}


class BusinessError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


@dataclass(frozen=True)
class Principal:
    user_id: str
    organization_id: str
    role: str


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BusinessError("invalid_payload", "Payload must contain finite JSON values") from exc


def _identifier(value: str, label: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BusinessError("invalid_input", f"{label} must be a nonempty string of at most {maximum} characters")
    return value.strip()


SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, email TEXT NOT NULL COLLATE NOCASE UNIQUE, display_name TEXT NOT NULL,
 password_hash TEXT NOT NULL, password_salt TEXT NOT NULL, password_iterations INTEGER NOT NULL,
 created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
 user_id TEXT NOT NULL REFERENCES users(id), organization_id TEXT NOT NULL REFERENCES organizations(id),
 role TEXT NOT NULL CHECK(role IN ('customer','support','kb_admin','admin')),
 PRIMARY KEY(user_id,organization_id)
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, organization_id TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL,
 FOREIGN KEY(user_id,organization_id) REFERENCES memberships(user_id,organization_id)
);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, created_by TEXT NOT NULL,
 task_type TEXT NOT NULL CHECK(task_type='ticket_draft'),
 status TEXT NOT NULL CHECK(status IN ('waiting_confirmation','completed','cancelled','rejected','expired')),
 revision INTEGER NOT NULL CHECK(revision>=1), payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
 confirmation_key TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL, ticket_id TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(id,organization_id),
 FOREIGN KEY(created_by,organization_id) REFERENCES memberships(user_id,organization_id),
 FOREIGN KEY(ticket_id,organization_id) REFERENCES tickets(id,organization_id)
);
CREATE TABLE IF NOT EXISTS tickets (
 id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, created_by TEXT NOT NULL,
 source_task_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL, description TEXT NOT NULL,
 category TEXT NOT NULL, priority TEXT NOT NULL, metadata_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','in_progress','resolved','closed','cancelled')),
 version INTEGER NOT NULL CHECK(version>=1), created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(id,organization_id),
 FOREIGN KEY(created_by,organization_id) REFERENCES memberships(user_id,organization_id),
 FOREIGN KEY(source_task_id,organization_id) REFERENCES tasks(id,organization_id)
);
CREATE TABLE IF NOT EXISTS approvals (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, organization_id TEXT NOT NULL,
 revision INTEGER NOT NULL, payload_hash TEXT NOT NULL, expires_at REAL NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','consumed','revoked','cancelled','rejected','expired')),
 created_at REAL NOT NULL, consumed_at REAL, consumed_by TEXT REFERENCES users(id),
 UNIQUE(task_id,revision),
 FOREIGN KEY(task_id,organization_id) REFERENCES tasks(id,organization_id)
);
CREATE TABLE IF NOT EXISTS operations (
 organization_id TEXT NOT NULL REFERENCES organizations(id), idempotency_key TEXT NOT NULL,
 actor_id TEXT NOT NULL REFERENCES users(id), operation_type TEXT NOT NULL,
 fingerprint TEXT NOT NULL, result_json TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(organization_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id TEXT NOT NULL REFERENCES organizations(id),
 actor_id TEXT NOT NULL REFERENCES users(id), task_id TEXT, ticket_id TEXT,
 event_type TEXT NOT NULL, details_json TEXT NOT NULL, created_at REAL NOT NULL,
 FOREIGN KEY(task_id,organization_id) REFERENCES tasks(id,organization_id),
 FOREIGN KEY(ticket_id,organization_id) REFERENCES tickets(id,organization_id)
);
CREATE TABLE IF NOT EXISTS conversation_ownership (
 conversation_id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 created_at REAL NOT NULL, UNIQUE(conversation_id,organization_id),
 FOREIGN KEY(owner_id,organization_id) REFERENCES memberships(user_id,organization_id)
);
CREATE TABLE IF NOT EXISTS request_ownership (
 request_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, organization_id TEXT NOT NULL,
 owner_id TEXT NOT NULL REFERENCES users(id), created_at REAL NOT NULL,
 FOREIGN KEY(conversation_id,organization_id) REFERENCES conversation_ownership(conversation_id,organization_id)
);
CREATE INDEX IF NOT EXISTS tasks_org_time ON tasks(organization_id,created_at);
CREATE INDEX IF NOT EXISTS tickets_org_time ON tickets(organization_id,created_at);
CREATE INDEX IF NOT EXISTS events_org_time ON events(organization_id,id);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
PRAGMA user_version=1;
"""


class BusinessStore:
    def __init__(self, db_path: str | Path, confirmation_ttl_seconds: int = 900,
                 now: Callable[[], float] = time.time):
        if str(db_path) == ":memory:":
            raise ValueError("Use a persistent file path; per-operation connections cannot share :memory:")
        if confirmation_ttl_seconds <= 0:
            raise ValueError("confirmation_ttl_seconds must be positive")
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.confirmation_ttl_seconds = confirmation_ttl_seconds
        self.now = now
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise BusinessError("unsupported_schema", "Unsupported business database schema", 500)
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _actor(self, conn: sqlite3.Connection, actor: Principal) -> str:
        if not isinstance(actor, Principal):
            raise BusinessError("unauthorized", "A server-authenticated principal is required", 401)
        row = conn.execute("SELECT role FROM memberships WHERE user_id=? AND organization_id=?",
                           (actor.user_id, actor.organization_id)).fetchone()
        if row is None or actor.role != row["role"]:
            raise BusinessError("forbidden", "Membership or role is no longer valid", 403)
        return row["role"]

    @staticmethod
    def _visible(row: sqlite3.Row | None, actor: Principal, role: str) -> sqlite3.Row:
        if row is None or row["organization_id"] != actor.organization_id:
            raise BusinessError("not_found", "Resource not found", 404)
        if role not in MANAGERS and row["created_by"] != actor.user_id:
            raise BusinessError("not_found", "Resource not found", 404)
        return row

    @staticmethod
    def _payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        if not isinstance(payload, dict) or set(payload) - {"title", "description", "category", "priority", "metadata"}:
            raise BusinessError("invalid_payload", "Only title, description, category, priority and metadata are accepted")
        result = {
            "title": _identifier(payload.get("title"), "title", 200),
            "description": _identifier(payload.get("description"), "description", 10000),
            "category": payload.get("category", "general"),
            "priority": payload.get("priority", "normal"),
            "metadata": payload.get("metadata", {}),
        }
        if not isinstance(result["category"], str) or result["category"] not in {"general", "technical", "billing"}:
            raise BusinessError("invalid_payload", "Unknown ticket category")
        if not isinstance(result["priority"], str) or result["priority"] not in {"low", "normal", "high", "urgent"}:
            raise BusinessError("invalid_payload", "Unknown ticket priority")
        if not isinstance(result["metadata"], dict) or len(_json(result["metadata"]).encode("utf-8")) > 16384:
            raise BusinessError("invalid_payload", "metadata must be an object no larger than 16 KiB")
        serialized = _json(result)
        return result, serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _task(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        return {
            "id": row["id"], "task_type": row["task_type"], "organization_id": row["organization_id"],
            "created_by": row["created_by"], "status": row["status"], "revision": row["revision"],
            "payload_hash": row["payload_hash"], "payload": payload,
            "draft": {"id": row["id"], "revision": row["revision"], "payload_hash": row["payload_hash"], "payload": payload},
            "confirmation_expires_at": row["expires_at"], "expires_at": row["expires_at"],
            "idempotency_key": row["confirmation_key"], "ticket_id": row["ticket_id"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    @staticmethod
    def _ticket(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def _event(self, conn: sqlite3.Connection, actor: Principal, event_type: str,
               task_id: str | None = None, ticket_id: str | None = None, **details: Any) -> None:
        conn.execute("INSERT INTO events(organization_id,actor_id,task_id,ticket_id,event_type,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                     (actor.organization_id, actor.user_id, task_id, ticket_id, event_type, _json(details), self.now()))

    def _approval(self, conn: sqlite3.Connection, task_id: str, org_id: str, revision: int,
                  payload_hash: str, expires_at: float, timestamp: float) -> None:
        conn.execute("INSERT INTO approvals(id,task_id,organization_id,revision,payload_hash,expires_at,status,created_at) VALUES(?,?,?,?,?,?,'pending',?)",
                     (uuid.uuid4().hex, task_id, org_id, revision, payload_hash, expires_at, timestamp))

    def _expire(self, conn: sqlite3.Connection, row: sqlite3.Row, actor: Principal) -> sqlite3.Row:
        if row["status"] == "waiting_confirmation" and self.now() >= row["expires_at"]:
            conn.execute("UPDATE tasks SET status='expired',updated_at=? WHERE id=?", (self.now(), row["id"]))
            conn.execute("UPDATE approvals SET status='expired' WHERE task_id=? AND status='pending'", (row["id"],))
            self._event(conn, actor, "task.expired", task_id=row["id"], revision=row["revision"])
            return conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
        return row

    @staticmethod
    def _fingerprint(actor: Principal, operation_type: str, **content: Any) -> str:
        return hashlib.sha256(_json({"actor_id": actor.user_id, "type": operation_type, **content}).encode("utf-8")).hexdigest()

    def _replay(self, conn: sqlite3.Connection, actor: Principal, key: str, fingerprint: str) -> dict[str, Any] | None:
        operation = conn.execute("SELECT * FROM operations WHERE organization_id=? AND idempotency_key=?",
                                 (actor.organization_id, key)).fetchone()
        if operation is None:
            return None
        if operation["actor_id"] != actor.user_id or operation["fingerprint"] != fingerprint:
            raise BusinessError("idempotency_conflict", "This idempotency key is bound to a different operation or payload", 409)
        result = json.loads(operation["result_json"])
        result["replayed"] = True
        return result

    def _operation(self, conn: sqlite3.Connection, actor: Principal, key: str, kind: str,
                   fingerprint: str, result: dict[str, Any]) -> None:
        conn.execute("INSERT INTO operations(organization_id,idempotency_key,actor_id,operation_type,fingerprint,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
                     (actor.organization_id, key, actor.user_id, kind, fingerprint, _json(result), self.now()))

    def create_draft(self, actor: Principal, payload: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        _, serialized, digest = self._payload(payload)
        key = _identifier(idempotency_key, "idempotency_key") if idempotency_key is not None else uuid.uuid4().hex
        fingerprint = self._fingerprint(actor, "draft.create", payload_hash=digest)
        with self._transaction() as conn:
            self._actor(conn, actor)
            replay = self._replay(conn, actor, key, fingerprint)
            if replay is not None:
                # Return live task state, not an obsolete waiting-confirmation snapshot.
                row = self._visible(conn.execute("SELECT * FROM tasks WHERE id=?", (replay["id"],)).fetchone(), actor, actor.role)
                result = self._task(self._expire(conn, row, actor))
                result["replayed"] = True
                return result
            task_id, timestamp = uuid.uuid4().hex, self.now()
            expires_at = timestamp + self.confirmation_ttl_seconds
            conn.execute("INSERT INTO tasks(id,organization_id,created_by,task_type,status,revision,payload_json,payload_hash,confirmation_key,expires_at,created_at,updated_at) VALUES(?,?,?,'ticket_draft','waiting_confirmation',1,?,?,?,?,?,?)",
                         (task_id, actor.organization_id, actor.user_id, serialized, digest, uuid.uuid4().hex, expires_at, timestamp, timestamp))
            self._approval(conn, task_id, actor.organization_id, 1, digest, expires_at, timestamp)
            self._event(conn, actor, "draft.created", task_id=task_id, revision=1, payload_hash=digest)
            result = self._task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            result["replayed"] = False
            self._operation(conn, actor, key, "draft.create", fingerprint, result)
            return result

    def get_task(self, actor: Principal, task_id: str) -> dict[str, Any]:
        with self._transaction() as conn:
            role = self._actor(conn, actor)
            row = self._visible(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone(), actor, role)
            return self._task(self._expire(conn, row, actor))

    def list_tasks(self, actor: Principal, limit: int = 50) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            role = self._actor(conn, actor)
            clause, params = self._list_scope(actor, role)
            rows = conn.execute(f"SELECT * FROM tasks WHERE {clause} ORDER BY created_at DESC,id LIMIT ?",
                                (*params, self._limit(limit))).fetchall()
            return [self._task(self._expire(conn, row, actor)) for row in rows]

    def revise_draft(self, actor: Principal, task_id: str, payload: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        _, serialized, digest = self._payload(payload)
        error = None
        result = None
        with self._transaction() as conn:
            role = self._actor(conn, actor)
            row = self._visible(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone(), actor, role)
            row = self._expire(conn, row, actor)
            if row["status"] != "waiting_confirmation":
                error = BusinessError("task_not_pending", f"Task is {row['status']}; no revision is permitted", 409)
            elif type(expected_revision) is not int or row["revision"] != expected_revision:
                error = BusinessError("revision_conflict", "Draft revision has changed", 409)
            else:
                revision, timestamp = row["revision"] + 1, self.now()
                expires_at = timestamp + self.confirmation_ttl_seconds
                conn.execute("UPDATE approvals SET status='revoked' WHERE task_id=? AND status='pending'", (task_id,))
                conn.execute("UPDATE tasks SET revision=?,payload_json=?,payload_hash=?,confirmation_key=?,expires_at=?,updated_at=? WHERE id=?",
                             (revision, serialized, digest, uuid.uuid4().hex, expires_at, timestamp, task_id))
                self._approval(conn, task_id, actor.organization_id, revision, digest, expires_at, timestamp)
                self._event(conn, actor, "draft.revised", task_id=task_id, previous_revision=row["revision"], revision=revision, payload_hash=digest)
                result = self._task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        if error:
            raise error
        return result

    def confirm_draft(self, actor: Principal, task_id: str, revision: int, payload_hash: str,
                      idempotency_key: str) -> dict[str, Any]:
        key = _identifier(idempotency_key, "idempotency_key")
        if type(revision) is not int or revision < 1 or not isinstance(payload_hash, str) or len(payload_hash) != 64:
            raise BusinessError("invalid_confirmation", "A valid revision and SHA-256 payload_hash are required")
        fingerprint = self._fingerprint(actor, "draft.confirm", task_id=task_id, revision=revision, payload_hash=payload_hash)
        error = None
        result = None
        with self._transaction() as conn:
            role = self._actor(conn, actor)
            row = self._visible(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone(), actor, role)
            replay = self._replay(conn, actor, key, fingerprint)
            if replay is not None:
                return replay
            row = self._expire(conn, row, actor)
            if row["revision"] != revision or row["payload_hash"] != payload_hash:
                error = BusinessError("revision_conflict", "Confirmation does not match the current draft revision and payload", 409)
            elif row["status"] == "completed":
                result = {"task": self._task(row), "ticket": self._ticket(conn.execute("SELECT * FROM tickets WHERE id=?", (row["ticket_id"],)).fetchone()), "replayed": True}
                self._operation(conn, actor, key, "draft.confirm", fingerprint, result)
            elif row["status"] != "waiting_confirmation":
                error = BusinessError("task_not_pending", f"Task is {row['status']}; confirmation is forbidden", 409)
            else:
                approval = conn.execute("SELECT * FROM approvals WHERE task_id=? AND revision=?", (task_id, revision)).fetchone()
                if (approval is None or approval["status"] != "pending" or approval["payload_hash"] != payload_hash
                        or self.now() >= approval["expires_at"]):
                    raise BusinessError("approval_invalid", "Approval is unavailable, expired or already consumed", 409)
                payload, ticket_id, timestamp = json.loads(row["payload_json"]), uuid.uuid4().hex, self.now()
                conn.execute("INSERT INTO tickets(id,organization_id,created_by,source_task_id,title,description,category,priority,metadata_json,status,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'open',1,?,?)",
                             (ticket_id, row["organization_id"], row["created_by"], task_id, payload["title"], payload["description"], payload["category"], payload["priority"], _json(payload["metadata"]), timestamp, timestamp))
                changed = conn.execute("UPDATE approvals SET status='consumed',consumed_at=?,consumed_by=? WHERE id=? AND status='pending'",
                                       (timestamp, actor.user_id, approval["id"])).rowcount
                if changed != 1:
                    raise BusinessError("approval_invalid", "Approval was already consumed", 409)
                conn.execute("UPDATE tasks SET status='completed',ticket_id=?,updated_at=? WHERE id=?", (ticket_id, timestamp, task_id))
                self._event(conn, actor, "draft.confirmed", task_id=task_id, ticket_id=ticket_id, revision=revision, payload_hash=payload_hash)
                self._event(conn, actor, "ticket.created", task_id=task_id, ticket_id=ticket_id, version=1)
                result = {"task": self._task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()),
                          "ticket": self._ticket(conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()), "replayed": False}
                self._operation(conn, actor, key, "draft.confirm", fingerprint, result)
        if error:
            raise error
        return result

    def _finish_task(self, actor: Principal, task_id: str, status: str,
                     expected_revision: int | None, reason: str = "") -> dict[str, Any]:
        error, result = None, None
        if not isinstance(reason, str) or len(reason) > 1000:
            raise BusinessError("invalid_input", "reason must be a string of at most 1000 characters")
        with self._transaction() as conn:
            role = self._actor(conn, actor)
            row = self._visible(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone(), actor, role)
            row = self._expire(conn, row, actor)
            if expected_revision is not None and (type(expected_revision) is not int or expected_revision != row["revision"]):
                error = BusinessError("revision_conflict", "Draft revision has changed", 409)
            elif row["status"] == status:
                result = self._task(row)
            elif row["status"] != "waiting_confirmation":
                error = BusinessError("task_not_pending", f"Task is {row['status']}", 409)
            else:
                conn.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, self.now(), task_id))
                conn.execute("UPDATE approvals SET status=? WHERE task_id=? AND status='pending'", (status, task_id))
                self._event(conn, actor, f"task.{status}", task_id=task_id, revision=row["revision"], reason=reason)
                result = self._task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        if error:
            raise error
        return result

    def cancel_task(self, actor: Principal, task_id: str, expected_revision: int | None = None) -> dict[str, Any]:
        return self._finish_task(actor, task_id, "cancelled", expected_revision)

    def reject_task(self, actor: Principal, task_id: str, expected_revision: int | None = None, reason: str = "") -> dict[str, Any]:
        return self._finish_task(actor, task_id, "rejected", expected_revision, reason)

    @staticmethod
    def _list_scope(actor: Principal, role: str) -> tuple[str, tuple[str, ...]]:
        if role in MANAGERS:
            return "organization_id=?", (actor.organization_id,)
        return "organization_id=? AND created_by=?", (actor.organization_id, actor.user_id)

    @staticmethod
    def _limit(value: int) -> int:
        if type(value) is not int or not 1 <= value <= 200:
            raise BusinessError("invalid_input", "limit must be an integer from 1 to 200")
        return value

    def get_ticket(self, actor: Principal, ticket_id: str) -> dict[str, Any]:
        with self._transaction() as conn:
            role = self._actor(conn, actor)
            return self._ticket(self._visible(conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone(), actor, role))

    def list_tickets(self, actor: Principal, limit: int = 50) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            role = self._actor(conn, actor)
            clause, params = self._list_scope(actor, role)
            return [self._ticket(row) for row in conn.execute(f"SELECT * FROM tickets WHERE {clause} ORDER BY created_at DESC,id LIMIT ?", (*params, self._limit(limit))).fetchall()]

    def update_ticket_status(self, actor: Principal, ticket_id: str, status: str, expected_version: int) -> dict[str, Any]:
        if not isinstance(status, str) or status not in TRANSITIONS:
            raise BusinessError("invalid_status", "Unknown ticket status")
        with self._transaction() as conn:
            role = self._actor(conn, actor)
            row = self._visible(conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone(), actor, role)
            if role not in MANAGERS:
                raise BusinessError("forbidden", "Only support or admin can change ticket status", 403)
            if type(expected_version) is not int or expected_version != row["version"]:
                raise BusinessError("version_conflict", "Ticket version has changed", 409)
            if status not in TRANSITIONS[row["status"]]:
                raise BusinessError("invalid_transition", f"Cannot move {row['status']} to {status}", 409)
            changed = conn.execute("UPDATE tickets SET status=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                                   (status, self.now(), ticket_id, expected_version)).rowcount
            if changed != 1:
                raise BusinessError("version_conflict", "Ticket version has changed", 409)
            self._event(conn, actor, "ticket.status_changed", task_id=row["source_task_id"], ticket_id=ticket_id,
                        previous_status=row["status"], status=status, previous_version=row["version"], version=row["version"] + 1)
            return self._ticket(conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())

    def list_events(self, actor: Principal, task_id: str | None = None,
                    ticket_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            role = self._actor(conn, actor)
            if task_id:
                self._visible(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone(), actor, role)
            if ticket_id:
                self._visible(conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone(), actor, role)
            clause, params = "e.organization_id=?", [actor.organization_id]
            if role not in MANAGERS:
                clause += " AND (e.task_id IN (SELECT id FROM tasks WHERE created_by=? AND organization_id=?) OR e.ticket_id IN (SELECT id FROM tickets WHERE created_by=? AND organization_id=?))"
                params.extend([actor.user_id, actor.organization_id, actor.user_id, actor.organization_id])
            if task_id:
                clause += " AND e.task_id=?"
                params.append(task_id)
            if ticket_id:
                clause += " AND e.ticket_id=?"
                params.append(ticket_id)
            rows = conn.execute(f"SELECT e.* FROM events e WHERE {clause} ORDER BY e.id DESC LIMIT ?", (*params, self._limit(limit))).fetchall()
            result = []
            for row in rows:
                event = dict(row)
                event["details"] = json.loads(event.pop("details_json"))
                result.append(event)
            return result

    def assign_membership(self, user_id: str, organization_id: str, role: str) -> dict[str, Any]:
        """Trusted administrative/CLI operation; never expose directly as a user API."""
        if role not in ROLES:
            raise BusinessError("invalid_role", "Unknown membership role")
        with self._transaction() as conn:
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() or not conn.execute("SELECT 1 FROM organizations WHERE id=?", (organization_id,)).fetchone():
                raise BusinessError("not_found", "User or organization not found", 404)
            conn.execute("INSERT INTO memberships(user_id,organization_id,role) VALUES(?,?,?) ON CONFLICT(user_id,organization_id) DO UPDATE SET role=excluded.role", (user_id, organization_id, role))
            return {"user_id": user_id, "organization_id": organization_id, "role": role}

    def _conversation(self, conn: sqlite3.Connection, actor: Principal, role: str, conversation_id: str,
                      create: bool = False) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM conversation_ownership WHERE conversation_id=?", (conversation_id,)).fetchone()
        if row is None and create:
            conn.execute("INSERT INTO conversation_ownership(conversation_id,organization_id,owner_id,created_at) VALUES(?,?,?,?)",
                         (conversation_id, actor.organization_id, actor.user_id, self.now()))
            row = conn.execute("SELECT * FROM conversation_ownership WHERE conversation_id=?", (conversation_id,)).fetchone()
        if row is None or row["organization_id"] != actor.organization_id or (row["owner_id"] != actor.user_id and role not in MANAGERS):
            raise BusinessError("not_found", "Conversation not found", 404)
        return row

    def claim_conversation(self, actor: Principal, conversation_id: str) -> dict[str, Any]:
        conversation_id = _identifier(conversation_id, "conversation_id")
        with self._transaction() as conn:
            role = self._actor(conn, actor)
            return dict(self._conversation(conn, actor, role, conversation_id, create=True))

    def bind_request(self, actor: Principal, request_id: str, conversation_id: str) -> dict[str, Any]:
        request_id = _identifier(request_id, "request_id")
        conversation_id = _identifier(conversation_id, "conversation_id")
        with self._transaction() as conn:
            role = self._actor(conn, actor)
            conversation = self._conversation(conn, actor, role, conversation_id)
            row = conn.execute("SELECT * FROM request_ownership WHERE request_id=?", (request_id,)).fetchone()
            if row is not None and (row["organization_id"] != actor.organization_id or row["conversation_id"] != conversation_id):
                raise BusinessError("request_conflict", "Request ID is already bound", 409)
            if row is None:
                conn.execute("INSERT INTO request_ownership(request_id,conversation_id,organization_id,owner_id,created_at) VALUES(?,?,?,?,?)",
                             (request_id, conversation_id, actor.organization_id, conversation["owner_id"], self.now()))
                row = conn.execute("SELECT * FROM request_ownership WHERE request_id=?", (request_id,)).fetchone()
            return dict(row)

    def can_read_request(self, actor: Principal, request_id: str) -> bool:
        with self._transaction() as conn:
            role = self._actor(conn, actor)
            row = conn.execute("SELECT * FROM request_ownership WHERE request_id=?", (request_id,)).fetchone()
            return bool(row and row["organization_id"] == actor.organization_id and (row["owner_id"] == actor.user_id or role in MANAGERS))

    def backup(self, destination: str | Path) -> dict[str, Any]:
        """Trusted maintenance operation. SQLite's backup API includes committed WAL data."""
        target_path = Path(destination).resolve()
        if target_path == self.db_path:
            raise BusinessError("invalid_backup", "Backup destination must differ from the live database")
        if target_path.exists():
            raise BusinessError("backup_exists", "Refusing to overwrite an existing backup", 409)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        source = self._connect()
        target = sqlite3.connect(str(target_path))
        try:
            source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise BusinessError("backup_failed", "Backup integrity check failed", 500)
        finally:
            source.close()
            target.close()
        return {"path": str(target_path), "integrity_check": "ok", "created_at": self.now()}
