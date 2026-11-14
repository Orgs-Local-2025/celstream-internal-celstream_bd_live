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


def status_snapshot() -> dict:
    """Non-sensitive status (counts, timestamps, run states) published as /status.json so
    office/VPN access can be validated without the UI. Never includes credentials,
    contact names or CRM payloads."""
    w = worker_state()
    active = active_run()
    last = db.one("SELECT id, status, finished_at FROM processing_runs WHERE status NOT IN"
                  " ('queued','running') ORDER BY id DESC LIMIT 1")
    counts = db.one("""SELECT (SELECT COUNT(*) FROM jobs) jobs,
        (SELECT COUNT(*) FROM capability_analysis WHERE status='success') jobs_analysed,
        (SELECT COUNT(*) FROM companies WHERE apollo_status != 'rejected') companies,
        (SELECT COUNT(*) FROM contacts WHERE apollo_status != 'rejected') contacts,
        (SELECT COUNT(*) FROM scoring_results s JOIN companies c ON c.key=s.company_key
            WHERE s.band='High' AND c.apollo_status != 'rejected') high_priority,
        (SELECT COUNT(*) FROM scoring_results s JOIN companies c ON c.key=s.company_key
            WHERE s.band='Medium' AND c.apollo_status != 'rejected') medium_priority""")
    return {
        "generated_at": db.now(),
        "worker": {"id": w.get("worker_id"), "state": w.get("state"),
                   "online": w["online"], "heartbeat_at": w.get("heartbeat_at")},
        "active_run": {k: active[k] for k in ("id", "status", "stage", "progress")}
        if active else None,
        "last_run": last,
        "counts": counts,
    }


def write_status_file(path) -> None:
    import json
    import os
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status_snapshot(), default=str), encoding="utf-8")
    os.replace(tmp, path)  # atomic: readers never see a partial file


def _cli(argv: list[str]) -> int:
    """Host-side operator commands (used by scripts/start-run.ps1):
        python -m bdlive.runs request [--max-jobs N] [--sync-existing] [--reprocess]
                                      [--job-id ID ...] [--reanalyse]
        python -m bdlive.runs status
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="bdlive.runs")
    sub = parser.add_subparsers(dest="command", required=True)
    req = sub.add_parser("request")
    req.add_argument("--max-jobs", type=int, default=1)
    req.add_argument("--sync-existing", action="store_true")
    req.add_argument("--reprocess", action="store_true")
    req.add_argument("--reanalyse", action="store_true")
    req.add_argument("--job-id", action="append", default=[])
    req.add_argument("--source", default="operator-cli")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    db.init()
    if args.command == "status":
        print(json.dumps(status_snapshot(), indent=1, default=str))
        return 0
    try:
        run_id = request_run({"max_jobs": args.max_jobs, "sync_existing": args.sync_existing,
                              "reprocess_existing": args.reprocess, "job_ids": args.job_id,
                              "reanalyse": args.reanalyse}, source=args.source)
    except RunAlreadyActive as exc:
        print(f"not started: run {exc.run_id} is already {exc.status}")
        return 2
    print(f"queued run {run_id}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_cli(sys.argv[1:]))
