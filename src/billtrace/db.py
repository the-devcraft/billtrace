"""Short-lived connections with explicit, serialized write transactions."""

import hashlib
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from importlib.resources import files
from pathlib import Path
from uuid import uuid4


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("Use a file database: each request owns a separate connection")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Unsupported schema version: {version}")
        with closing(self.connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(files("billtrace").joinpath("schema.sql").read_text())
            # Fixed internal table identifiers only; never interpolate user input into SQL.
            for table in (
                "customers",
                "usage_events",
                "invoices",
                "statements",
                "payments",
                "reconciliation_runs",
                "audit_events",
            ):
                for operation in ("UPDATE", "DELETE"):
                    conn.execute(
                        f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} "
                        f"BEFORE {operation} ON {table} BEGIN "
                        "SELECT RAISE(ABORT, 'immutable record'); END;"  # noqa: S608
                    )

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def provision(self, name: str) -> tuple[str, str]:
        if not name.strip() or len(name) > 100:
            raise ValueError("Tenant name must contain 1–100 characters")
        tenant_id, key = str(uuid4()), "bt_" + secrets.token_urlsafe(32)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO tenants(id, name, key_hash) VALUES (?, ?, ?)",
                (tenant_id, name, key_hash(key)),
            )
        return tenant_id, key

    def authenticate(self, key: str) -> str | None:
        with self.transaction(write=False) as conn:
            row = conn.execute(
                "SELECT id FROM tenants WHERE key_hash = ? AND revoked = 0", (key_hash(key),)
            ).fetchone()
            return str(row["id"]) if row else None

    def revoke(self, tenant_id: str) -> bool:
        with self.transaction() as conn:
            return (
                conn.execute("UPDATE tenants SET revoked = 1 WHERE id = ?", (tenant_id,)).rowcount
                == 1
            )
