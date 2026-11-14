"""SQLite persistence. Deliberately small: one table per essential entity."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from bdlive.settings import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS processing_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,              -- running | success | partial | failed
    stage TEXT,
    params_json TEXT,
    summary_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,               -- theirstack:<job id>
    theirstack_id TEXT NOT NULL,
    first_run_id INTEGER,
    last_run_id INTEGER,
    title TEXT NOT NULL,
    company_name TEXT NOT NULL,
    company_domain TEXT,
    company_key TEXT NOT NULL,
    location TEXT,
    country TEXT,
    source_url TEXT,
    posted_at TEXT,
    seniority TEXT,
    employment_type TEXT,
    remote INTEGER,
    technologies_json TEXT,
    description TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    analysis_status TEXT NOT NULL DEFAULT 'pending'   -- pending | success | failed
);
CREATE TABLE IF NOT EXISTS companies (
    key TEXT PRIMARY KEY,              -- domain, or theirstack company id when no domain
    name TEXT NOT NULL,
    domain TEXT,
    country TEXT,
    industry TEXT,
    employees INTEGER,
    linkedin_url TEXT,
    theirstack_company_id TEXT,
    last_run_id INTEGER,
    apollo_org_id TEXT,
    apollo_account_id TEXT,
    apollo_status TEXT NOT NULL DEFAULT 'pending',
    apollo_error TEXT,
    apollo_synced_at TEXT,
    hubspot_id TEXT,
    hubspot_status TEXT NOT NULL DEFAULT 'pending',
    hubspot_error TEXT,
    hubspot_synced_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_key TEXT NOT NULL REFERENCES companies(key),
    apollo_person_id TEXT UNIQUE,
    apollo_contact_id TEXT,
    first_name TEXT,
    last_name TEXT,
    title TEXT,
    role_category TEXT,
    email TEXT,
    email_status TEXT,
    linkedin_url TEXT,
    last_run_id INTEGER,
    apollo_status TEXT NOT NULL DEFAULT 'pending',
    apollo_error TEXT,
    apollo_synced_at TEXT,
    hubspot_id TEXT,
    hubspot_status TEXT NOT NULL DEFAULT 'pending',
    hubspot_error TEXT,
    hubspot_associated INTEGER NOT NULL DEFAULT 0,
    hubspot_synced_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capability_analysis (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    run_id INTEGER,
    status TEXT NOT NULL,              -- success | failed
    model TEXT,
    prompt_version TEXT,
    summary TEXT,
    seniority TEXT,
    technologies_json TEXT,
    skills_json TEXT,
    responsibilities_json TEXT,
    matches_json TEXT,                 -- validated capability matches
    rejected_matches_json TEXT,        -- matches whose evidence was not in the posting
    personas_json TEXT,
    caveats_json TEXT,
    confidence REAL,
    capability_score REAL,             -- 0..100 job-level capability matching score
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    error TEXT,
    analysed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scoring_results (
    company_key TEXT PRIMARY KEY REFERENCES companies(key),
    run_id INTEGER,
    total REAL NOT NULL,
    band TEXT NOT NULL,
    components_json TEXT,
    rationale_json TEXT,
    matched_capabilities_json TEXT,
    relevant_job_ids_json TEXT,
    rules_version TEXT,
    scored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS integration_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    provider TEXT NOT NULL,
    operation TEXT NOT NULL,
    method TEXT,
    endpoint TEXT,
    attempt INTEGER,
    http_status INTEGER,
    success INTEGER NOT NULL,
    duration_ms INTEGER,
    entity TEXT,
    external_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_run ON integration_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_key);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company_key);
"""

_lock = threading.RLock()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(value, default=None):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def connect() -> sqlite3.Connection:
    """Autocommit connection; writes go through tx(), which takes the write lock up front.
    WAL + busy_timeout let the portal and worker processes (containers) share one file."""
    path = settings().database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA_VERSION = 2

# Columns added after the first live runs; applied in order, each guarded.
MIGRATIONS = {
    1: [("processing_runs", "progress", "REAL DEFAULT 0")],
    2: [("processing_runs", "requested_at", "TEXT"),
        ("processing_runs", "trigger_source", "TEXT"),
        ("processing_runs", "worker_id", "TEXT"),
        ("processing_runs", "heartbeat_at", "TEXT")],
}
WORKER_TABLE = """
CREATE TABLE IF NOT EXISTS worker_status (
    worker_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,               -- idle | running | stopping
    current_run_id INTEGER,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    version TEXT
);
"""


def backup(target_dir: Path | None = None, label: str = "manual") -> Path:
    """Consistent online copy via the SQLite backup API (safe while WAL is active)."""
    source_path = settings().database_path
    target_dir = Path(target_dir or os.environ.get("LIVE_BACKUP_DIR")
                      or source_path.parent / "backups")
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"{source_path.stem}-{label}-{stamp}.db"
    src = connect()
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def init() -> None:
    conn = connect()
    try:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.executescript(SCHEMA + WORKER_TABLE)
        pending = []
        for step in range(1, SCHEMA_VERSION + 1):
            for table, column, ddl in MIGRATIONS.get(step, []):
                columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    pending.append((table, column, ddl))
        if pending and "processing_runs" in existing:
            # Never alter a populated database without a restorable copy first.
            conn.close()
            backup(label=f"pre-migration-v{version}-to-v{SCHEMA_VERSION}")
            conn = connect()
        for table, column, ddl in pending:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        if version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()


@contextmanager
def tx():
    with _lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")  # take the write lock now: no upgrade deadlocks
            yield conn
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def query(sql: str, params=()) -> list[dict]:
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def one(sql: str, params=()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def update(table: str, key_col: str, key, **values) -> None:
    if not values:
        return
    cols = ", ".join(f"{name} = ?" for name in values)
    with tx() as conn:
        conn.execute(
            f"UPDATE {table} SET {cols} WHERE {key_col} = ?", (*values.values(), key)
        )
