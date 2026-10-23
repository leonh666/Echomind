"""Local username/password authentication and customer-only public signup.

Create an initial admin with:
  python -m business.auth --db data/business.sqlite3 create-user --email you@example.invalid --organization-name Demo
The password is always requested with getpass; it is never accepted in argv.
Administrative roles require trusted CLI access; signup always creates customers.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from typing import Any

from .store import BusinessError, BusinessStore, Principal, ROLES, _identifier


ITERATIONS = 600000


def normalize_username(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,32}", value.strip()):
        raise BusinessError("invalid_username", "用户名须为 3–32 位字母、数字或下划线")
    return value.strip().casefold()


def migrate_usernames(conn):
    if "username" not in {row[1] for row in conn.execute("PRAGMA table_info(users)")}:
        conn.execute("ALTER TABLE users ADD COLUMN username TEXT COLLATE NOCASE")
    used = {r[0].casefold() for r in conn.execute("SELECT username FROM users WHERE username IS NOT NULL")}
    for row in conn.execute("SELECT id,email FROM users WHERE username IS NULL ORDER BY created_at,id").fetchall():
        base = re.sub(r"[^a-z0-9_]", "_", row["email"].split("@")[0].casefold())[:32]
        if len(base) < 3:
            base = "user_" + hashlib.sha256(row["id"].encode()).hexdigest()[:12]
        candidate, suffix = base, 1
        while candidate in used:
            tail = "_" + str(suffix)
            candidate = base[:32-len(tail)] + tail
            suffix += 1
        conn.execute("UPDATE users SET username=? WHERE id=?", (candidate, row["id"]))
        used.add(candidate)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_username_unique ON users(username COLLATE NOCASE)")


class AuthService:
    def __init__(self, store: BusinessStore, session_ttl_seconds: int = 86400):
        if session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be positive")
        self.store, self.session_ttl_seconds = store, session_ttl_seconds

    @staticmethod
    def _password(password: str, salt: str, iterations: int = ITERATIONS) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations).hex()

    def create_user(self, email: str, password: str, organization_name: str = "",
                    display_name: str = "", organization_id: str | None = None,
                    role: str | None = None, username: str | None = None) -> dict[str, Any]:
        """Trusted bootstrap/CLI action. An existing organization must be explicitly named."""
        email = _identifier(email, "email", 254).casefold()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise BusinessError("invalid_email", "A valid email address is required")
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise BusinessError("invalid_password", "Password must contain 12 to 1024 characters")
        display_name = _identifier(display_name or email.split("@")[0], "display_name", 120)
        role = role or ("customer" if organization_id else "admin")
        if role not in ROLES:
            raise BusinessError("invalid_role", "Unknown membership role")
        if organization_id is None:
            organization_name = _identifier(organization_name or f"{display_name}'s workspace", "organization_name", 160)
        elif organization_name:
            raise BusinessError("invalid_input", "Specify either organization_id or a new organization_name")
        username = normalize_username(username) if username is not None else None
        salt = secrets.token_hex(32)
        password_hash = self._password(password, salt)
        user_id, timestamp = uuid.uuid4().hex, self.store.now()
        try:
            with self.store._transaction() as conn:
                if organization_id is None:
                    organization_id = uuid.uuid4().hex
                    conn.execute("INSERT INTO organizations(id,name,created_at) VALUES(?,?,?)", (organization_id, organization_name, timestamp))
                organization = conn.execute("SELECT * FROM organizations WHERE id=?", (organization_id,)).fetchone()
                if organization is None:
                    raise BusinessError("not_found", "Organization not found", 404)
                conn.execute("INSERT INTO users(id,email,display_name,password_hash,password_salt,password_iterations,created_at) VALUES(?,?,?,?,?,?,?)",
                             (user_id, email, display_name, password_hash, salt, ITERATIONS, timestamp))
                if username is None:
                    migrate_usernames(conn)
                    username = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()[0]
                else:
                    conn.execute("UPDATE users SET username=? WHERE id=?", (username, user_id))
                conn.execute("INSERT INTO memberships(user_id,organization_id,role) VALUES(?,?,?)", (user_id, organization_id, role))
                return {"user": {"id": user_id, "email": email, "display_name": display_name, "username": username},
                        "organization": dict(organization), "organization_id": organization_id, "role": role}
        except sqlite3.IntegrityError as exc:
            raise BusinessError("user_exists", "用户名或邮箱已被使用", 409) from exc

    def register(self, username: str, password: str, organization_id: str | None = None):
        username = normalize_username(username)
        # The target comes only from server configuration, never from the body.
        if organization_id is None:
            organization_id = "self-service"
            with self.store._transaction() as conn:
                conn.execute("INSERT OR IGNORE INTO organizations(id,name,created_at) VALUES(?,?,?)",
                             (organization_id, "EchoMind 客户工作区", self.store.now()))
        return self.create_user(uuid.uuid4().hex + "@accounts.invalid", password,
                                display_name=username, organization_id=organization_id,
                                role="customer", username=username)

    def login(self, email: str, password: str, organization_id: str | None = None) -> dict[str, Any]:
        if not isinstance(email, str) or not isinstance(password, str) or len(password) > 1024:
            raise BusinessError("invalid_credentials", "用户名或密码错误", 401)
        # Password derivation runs outside a write transaction so failed logins do
        # not hold the business database's writer lock for the PBKDF2 duration.
        conn = self.store._connect()
        try:
            user = conn.execute("SELECT * FROM users WHERE username=? OR email=?", (email.strip().casefold(), email.strip().casefold())).fetchone()
        finally:
            conn.close()
        salt = user["password_salt"] if user else "00" * 32
        calculated = self._password(password, salt, user["password_iterations"] if user else ITERATIONS)
        if user is None or not hmac.compare_digest(calculated, user["password_hash"]):
            raise BusinessError("invalid_credentials", "用户名或密码错误", 401)
        with self.store._transaction() as conn:
            if organization_id is None:
                membership = conn.execute("SELECT * FROM memberships WHERE user_id=? ORDER BY organization_id LIMIT 1", (user["id"],)).fetchone()
            else:
                membership = conn.execute("SELECT * FROM memberships WHERE user_id=? AND organization_id=?", (user["id"], organization_id)).fetchone()
            if membership is None:
                raise BusinessError("forbidden", "No membership for the requested organization", 403)
            token, timestamp = secrets.token_urlsafe(48), self.store.now()
            expires_at = timestamp + self.session_ttl_seconds
            conn.execute("INSERT INTO sessions(token_hash,user_id,organization_id,created_at,expires_at) VALUES(?,?,?,?,?)",
                         (hashlib.sha256(token.encode("utf-8")).hexdigest(), user["id"], membership["organization_id"], timestamp, expires_at))
            return {"token": token, "expires_at": expires_at,
                    "user": {"id": user["id"], "email": user["email"], "display_name": user["display_name"], "username": user["username"]},
                    "organization_id": membership["organization_id"], "role": membership["role"]}

    def authenticate(self, token: str, organization_id: str | None = None) -> Principal:
        if not isinstance(token, str) or not token or len(token) > 512:
            raise BusinessError("unauthorized", "A valid session is required", 401)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.store._transaction() as conn:
            row = conn.execute("SELECT s.*,m.role FROM sessions s JOIN memberships m ON m.user_id=s.user_id AND m.organization_id=s.organization_id WHERE token_hash=?", (digest,)).fetchone()
            if row is None or row["revoked_at"] is not None or self.store.now() >= row["expires_at"]:
                raise BusinessError("unauthorized", "Session is invalid or expired", 401)
            if organization_id is not None and row["organization_id"] != organization_id:
                raise BusinessError("forbidden", "Session belongs to another organization", 403)
            return Principal(row["user_id"], row["organization_id"], row["role"])

    def logout(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or len(token) > 512:
            raise BusinessError("unauthorized", "Invalid session token", 401)
        with self.store._transaction() as conn:
            conn.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                         (self.store.now(), hashlib.sha256(token.encode("utf-8")).hexdigest()))
        return {"logged_out": True}

    def profile(self, actor: Principal) -> dict[str, Any]:
        """Return only the authenticated member's public account fields."""
        with self.store._transaction() as conn:
            self.store._actor(conn, actor)
            user = conn.execute(
                "SELECT id,email,username,display_name,created_at FROM users WHERE id=?",
                (actor.user_id,),
            ).fetchone()
            organization = conn.execute(
                "SELECT id,name,created_at FROM organizations WHERE id=?",
                (actor.organization_id,),
            ).fetchone()
            return {"user": dict(user), "organization": dict(organization)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Trusted local EchoMind business account administration")
    parser.add_argument("--db", required=True, help="Persistent SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-user", help="Create a user; password is read interactively")
    create.add_argument("--email", required=True)
    create.add_argument("--username", help="Unique login name, 3–32 letters, digits or underscores")
    create.add_argument("--display-name", default="")
    target = create.add_mutually_exclusive_group(required=True)
    target.add_argument("--organization-name")
    target.add_argument("--organization-id")
    create.add_argument("--role", choices=sorted(ROLES))
    args = parser.parse_args()
    password = getpass.getpass("Password (at least 12 characters): ")
    if password != getpass.getpass("Repeat password: "):
        parser.error("Passwords do not match")
    try:
        result = AuthService(BusinessStore(args.db)).create_user(
            args.email, password, organization_name=args.organization_name or "", display_name=args.display_name,
            organization_id=args.organization_id, role=args.role, username=args.username)
    except BusinessError as exc:
        parser.exit(1, f"{exc.code}: {exc.message}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
