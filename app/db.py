"""SQLite persistence for factor versions and immutable snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS factor_versions (
    version     TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS factors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    version          TEXT NOT NULL REFERENCES factor_versions(version),
    ref              TEXT NOT NULL,
    name             TEXT,
    unit             TEXT NOT NULL,
    kg_co2e_per_unit TEXT NOT NULL,
    UNIQUE (version, ref)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id                TEXT PRIMARY KEY,
    input_hash        TEXT NOT NULL,
    client_request_id TEXT,
    factor_version    TEXT NOT NULL,
    allocation_method TEXT NOT NULL,
    source_snapshot   TEXT,
    request_json      TEXT NOT NULL,
    result_json       TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_input_hash
    ON snapshots(input_hash);
CREATE INDEX IF NOT EXISTS idx_snapshots_client
    ON snapshots(client_request_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_source
    ON snapshots(source_snapshot);
CREATE INDEX IF NOT EXISTS idx_factors_version ON factors(version);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    first_time = not os.path.exists(db_path) if db_path != ":memory:" else False
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    if first_time:
        conn.executescript(SCHEMA)
        conn.commit()
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# -------- factor versions --------------------------------------------------
#
# Factor versions are append-only evidence, just like snapshots: there is no
# UPDATE/REPLACE path.  Re-registering an existing version goes through the
# service layer, which either confirms an exact idempotent resubmission or
# rejects the write with 409 — stored factor rows are never rewritten.

def factor_version_exists(conn: sqlite3.Connection, version: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM factor_versions WHERE version = ?",
        (version,)).fetchone() is not None


def insert_factor_version(conn: sqlite3.Connection, version: str,
                          factors: List[Dict[str, Any]]) -> None:
    """Insert a brand-new version and all its factor rows.

    Must run inside a caller-managed transaction.  A duplicate version or
    factor ref raises ``sqlite3.IntegrityError`` instead of replacing
    anything.
    """
    conn.execute(
        "INSERT INTO factor_versions(version, created_at) VALUES (?, ?)",
        (version, utcnow()))
    conn.executemany(
        "INSERT INTO factors(version, ref, name, unit, kg_co2e_per_unit) "
        "VALUES (?, ?, ?, ?, ?)",
        [(version, f["ref"], f.get("name"), f["unit"],
          str(f["kg_co2e_per_unit"])) for f in factors])


def list_factor_versions(conn: sqlite3.Connection) -> List[str]:
    return [r["version"] for r in conn.execute(
        "SELECT version FROM factor_versions ORDER BY version")]


def get_factors(conn: sqlite3.Connection, version: str
                ) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute(
        "SELECT ref, name, unit, kg_co2e_per_unit FROM factors "
        "WHERE version = ? ORDER BY ref", (version,)).fetchall()
    return {r["ref"]: {"ref": r["ref"], "name": r["name"], "unit": r["unit"],
                       "kg_co2e_per_unit": r["kg_co2e_per_unit"]}
            for r in rows}


# -------- snapshots --------------------------------------------------------

def insert_snapshot(conn: sqlite3.Connection, *, snapshot_id: str,
                    input_hash: str, client_request_id: Optional[str],
                    factor_version: str, allocation_method: str,
                    source_snapshot: Optional[str],
                    request_json: str, result_json: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO snapshots(id, input_hash, client_request_id, "
            "factor_version, allocation_method, source_snapshot, "
            "request_json, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (snapshot_id, input_hash, client_request_id, factor_version,
             allocation_method, source_snapshot, request_json, result_json,
             utcnow()))


def get_snapshot(conn: sqlite3.Connection, snapshot_id: str
                 ) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    return dict(row) if row else None


def get_snapshot_by_hash(conn: sqlite3.Connection, input_hash: str
                         ) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM snapshots WHERE input_hash = ?",
        (input_hash,)).fetchone()
    return dict(row) if row else None


def get_snapshot_by_client_id(conn: sqlite3.Connection, client_request_id: str
                              ) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM snapshots WHERE client_request_id = ? "
        "ORDER BY created_at, id LIMIT 1",
        (client_request_id,)).fetchone()
    return dict(row) if row else None


def list_snapshots(conn: sqlite3.Connection, limit: int = 100,
                   offset: int = 0) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, input_hash, client_request_id, factor_version, "
        "allocation_method, source_snapshot, created_at "
        "FROM snapshots ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def parse_result(row: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(row["result_json"])
