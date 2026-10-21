"""Trusted, local backup tooling. No network access or model calls."""
import argparse
import json
import sqlite3
from pathlib import Path

from .store import BusinessError, BusinessStore


def inspect(path):
    source = Path(path).resolve()
    if not source.is_file():
        raise ValueError("Database file does not exist")
    conn = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("organizations", "users", "tasks", "tickets", "events")}
        if integrity != "ok" or foreign_keys:
            raise ValueError("Database integrity or foreign-key check failed")
        return {"integrity_check": integrity, "foreign_key_check": "ok", "counts": counts}
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["backup", "verify"])
    parser.add_argument("--db", required=True)
    parser.add_argument("--destination")
    args = parser.parse_args()
    try:
        if args.command == "verify":
            result = inspect(args.db)
        else:
            if not args.destination:
                parser.error("backup requires --destination")
            # Do not accidentally create a fresh, empty database due to a typo.
            inspect(args.db)
            result = BusinessStore(args.db).backup(args.destination)
            result["verification"] = inspect(args.destination)
    except (BusinessError, ValueError, sqlite3.DatabaseError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
