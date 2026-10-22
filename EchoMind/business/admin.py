"""Organization-scoped, read-only administrative projections of business state.

The caller supplies a server-authenticated Principal. Every method revalidates the
current membership in the same transaction as its reads; a stale/forged admin role
does not grant access. These projections never query passwords or session tables,
and conversation results contain ownership metadata rather than chat content.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .store import BusinessError, BusinessStore, Principal


class AdminService:
    def __init__(self, store: BusinessStore):
        self.store = store

    def _require_admin(self, conn: sqlite3.Connection, actor: Principal) -> None:
        if self.store._actor(conn, actor) != "admin":
            raise BusinessError("forbidden", "Only organization admins can access backend records", 403)

    @staticmethod
    def _pagination(limit: int, offset: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BusinessError("invalid_input", "limit must be an integer from 1 to 100")
        if type(offset) is not int or offset < 0:
            raise BusinessError("invalid_input", "offset must be a nonnegative integer")
        # SQLite binds signed 64-bit integers; reject out-of-range values cleanly.
        if offset > 9223372036854775807:
            raise BusinessError("invalid_input", "offset exceeds the supported integer range")

    @staticmethod
    def _page(items: list[dict[str, Any]], total: int, limit: int, offset: int) -> dict[str, Any]:
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def overview(self, actor: Principal) -> dict[str, Any]:
        """Return persisted counts/statuses; do not expire or otherwise mutate tasks."""
        with self.store._transaction() as conn:
            self._require_admin(conn, actor)
            organization_id = actor.organization_id
            counts = {
                "users": conn.execute("SELECT COUNT(*) FROM memberships WHERE organization_id=?", (organization_id,)).fetchone()[0]
            }
            # Table names are a closed, local mapping; organization is always bound.
            for label, table in (("tasks", "tasks"), ("tickets", "tickets"), ("events", "events"),
                                 ("conversations", "conversation_ownership"), ("requests", "request_ownership")):
                counts[label] = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE organization_id=?", (organization_id,)).fetchone()[0]
            task_status = {
                row["status"]: row["total"] for row in conn.execute(
                    "SELECT status,COUNT(*) AS total FROM tasks WHERE organization_id=? GROUP BY status ORDER BY status",
                    (organization_id,),
                )
            }
            ticket_status = {
                row["status"]: row["total"] for row in conn.execute(
                    "SELECT status,COUNT(*) AS total FROM tickets WHERE organization_id=? GROUP BY status ORDER BY status",
                    (organization_id,),
                )
            }
            return {"organization_id": organization_id, "counts": counts,
                    "task_status": task_status, "ticket_status": ticket_status}

    def users(self, actor: Principal, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        with self.store._transaction() as conn:
            self._require_admin(conn, actor)
            self._pagination(limit, offset)
            total = conn.execute("SELECT COUNT(*) FROM memberships WHERE organization_id=?", (actor.organization_id,)).fetchone()[0]
            rows = conn.execute(
                "SELECT u.id,u.email,u.display_name,m.role,u.created_at "
                "FROM users u JOIN memberships m ON m.user_id=u.id "
                "WHERE m.organization_id=? ORDER BY u.created_at DESC,u.id ASC LIMIT ? OFFSET ?",
                (actor.organization_id, limit, offset),
            ).fetchall()
            return self._page([dict(row) for row in rows], total, limit, offset)

    def activity(self, actor: Principal, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        with self.store._transaction() as conn:
            self._require_admin(conn, actor)
            self._pagination(limit, offset)
            total = conn.execute("SELECT COUNT(*) FROM events WHERE organization_id=?", (actor.organization_id,)).fetchone()[0]
            rows = conn.execute(
                "SELECT e.id,e.organization_id,e.actor_id,u.display_name AS actor_display_name,"
                "u.email AS actor_email,e.task_id,e.ticket_id,e.event_type,e.details_json,e.created_at "
                "FROM events e JOIN users u ON u.id=e.actor_id "
                "WHERE e.organization_id=? ORDER BY e.id DESC LIMIT ? OFFSET ?",
                (actor.organization_id, limit, offset),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                details = json.loads(item.pop("details_json"))
                allowed = {"revision", "previous_revision", "version", "from_status", "to_status",
                           "status", "previous_status"}
                item["details"] = {key: value for key, value in details.items()
                                   if key in allowed and isinstance(value, (str, int, float, bool))}
                items.append(item)
            return self._page(items, total, limit, offset)

    def conversations(self, actor: Principal, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        with self.store._transaction() as conn:
            self._require_admin(conn, actor)
            self._pagination(limit, offset)
            total = conn.execute("SELECT COUNT(*) FROM conversation_ownership WHERE organization_id=?", (actor.organization_id,)).fetchone()[0]
            rows = conn.execute(
                "SELECT c.conversation_id,c.organization_id,c.owner_id,c.created_at,"
                "u.email AS owner_email,u.display_name AS owner_display_name,"
                "COUNT(r.request_id) AS request_count,MAX(r.created_at) AS last_request_at "
                "FROM conversation_ownership c JOIN users u ON u.id=c.owner_id "
                "LEFT JOIN request_ownership r ON r.conversation_id=c.conversation_id AND r.organization_id=c.organization_id "
                "WHERE c.organization_id=? "
                "GROUP BY c.conversation_id,c.organization_id,c.owner_id,c.created_at,u.email,u.display_name "
                "ORDER BY c.created_at DESC,c.conversation_id ASC LIMIT ? OFFSET ?",
                (actor.organization_id, limit, offset),
            ).fetchall()
            return self._page([dict(row) for row in rows], total, limit, offset)

    def _business_page(self, actor: Principal, table: str, limit: int, offset: int) -> dict[str, Any]:
        # Closed table mapping, bound scope and pagination. Administrative reads
        # never execute expiry transitions or expose confirmation capabilities.
        with self.store._transaction() as conn:
            self._require_admin(conn, actor)
            self._pagination(limit, offset)
            total = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE organization_id=?",
                                 (actor.organization_id,)).fetchone()[0]
            rows = conn.execute(f"SELECT * FROM {table} WHERE organization_id=? "
                                "ORDER BY created_at DESC,id ASC LIMIT ? OFFSET ?",
                                (actor.organization_id, limit, offset)).fetchall()
            items = []
            for row in rows:
                if table == "tasks":
                    value = self.store._task(row)
                    value["payload"] = {key: value["payload"][key] for key in
                                        ("title", "description", "category", "priority")}
                    keys = ("id", "created_by", "status", "revision", "payload", "ticket_id",
                            "expires_at", "confirmation_expires_at", "created_at", "updated_at")
                else:
                    value = self.store._ticket(row)
                    keys = ("id", "created_by", "status", "title", "description", "category",
                            "priority", "version", "source_task_id", "created_at", "updated_at")
                items.append({key: value[key] for key in keys})
            return self._page(items, total, limit, offset)

    def tasks(self, actor: Principal, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return self._business_page(actor, "tasks", limit, offset)

    def tickets(self, actor: Principal, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return self._business_page(actor, "tickets", limit, offset)
