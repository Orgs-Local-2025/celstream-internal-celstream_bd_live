"""SQLite-backed run queue shared by the portal (producer) and worker (consumer).

processing_runs.status lifecycle:  queued -> running -> success | partial | failed
At most one run may be queued or running at a time (enforced inside a write lock).
"""

from __future__ import annotations

from datetime import UTC, datetime

from bdlive import db

ACTIVE = ("queued", "running")
HEARTBEAT_STALE_SECONDS = 60


class RunAlreadyActive(RuntimeError):
    def __init__(self, run_id: int, status: str):
        self.run_id, self.status = run_id, status
        super().__init__(f"run {run_id} is already {status}")


def request_run(params: dict, source: str = "portal") -> int:
    """Queue a workflow run. Raises RunAlreadyActive instead of creating a duplicate."""
    with db.tx() as conn:
        active = conn.execute("SELECT id, status FROM processing_runs WHERE status IN"
                              " ('queued','running') ORDER BY id LIMIT 1").fetchone()
        if active:
            raise RunAlreadyActive(active["id"], active["status"])
        return conn.execute(
            "INSERT INTO processing_runs (started_at, requested_at, status, stage, params_json,"
            " trigger_source, progress) VALUES (?,?,?,?,?,?,0)",
            (db.now(), db.now(), "queued", "waiting for worker", db.dumps(params), source),
        ).lastrowid


def claim_next(worker_id: str) -> dict | None:
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM processing_runs WHERE status='queued' ORDER BY id"
                           " LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE processing_runs SET status='running', started_at=?, worker_id=?,"
                     " heartbeat_at=?, stage='claimed by worker' WHERE id=? AND"
                     " status='queued'", (db.now(), worker_id, db.now(), row["id"]))
        return dict(row) | {"params": db.loads(row["params_json"], {})}


def recover_interrupted(worker_id: str) -> list[int]:
    """Called once at worker start: any run still 'running' was interrupted (container
    stop/crash). Mark it failed; every stage is idempotent, so it is safe to queue again."""
    with db.tx() as conn:
        rows = conn.execute("SELECT id FROM processing_runs WHERE status='running'").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE processing_runs SET status='failed', finished_at=?, stage='interrupted',"
                " error=COALESCE(error || '; ', '') || ? WHERE id=?",
                (db.now(), f"interrupted: worker {worker_id} restarted before the run finished;"
                           " safe to re-run (all stages are idempotent)", row["id"]))
        return [r["id"] for r in rows]


def heartbeat(worker_id: str, state: str, run_id: int | None, started_at: str,
              version: str = "") -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO worker_status (worker_id, state, current_run_id, started_at,"
            " heartbeat_at, version) VALUES (?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET"
            " state=excluded.state, current_run_id=excluded.current_run_id,"
            " heartbeat_at=excluded.heartbeat_at, version=excluded.version",
            (worker_id, state, run_id, started_at, db.now(), version))
        if run_id:
            conn.execute("UPDATE processing_runs SET heartbeat_at=? WHERE id=?",
                         (db.now(), run_id))


def age_seconds(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds()
    except ValueError:
        return None


def worker_state() -> dict:
    """Latest worker heartbeat, with an explicit 'online' flag so stale state is never
    presented as current."""
    row = db.one("SELECT * FROM worker_status ORDER BY heartbeat_at DESC LIMIT 1")
    if not row:
        return {"online": False, "state": "never seen", "age": None}
    age = age_seconds(row["heartbeat_at"])
    return {**row, "age": age, "online": age is not None and age <= HEARTBEAT_STALE_SECONDS}


def active_run() -> dict | None:
    return db.one("SELECT * FROM processing_runs WHERE status IN ('queued','running')"
                  " ORDER BY id DESC LIMIT 1")
