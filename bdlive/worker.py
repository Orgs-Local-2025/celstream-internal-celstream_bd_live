"""Background worker: executes queued workflow runs from the shared SQLite database.

    python -m bdlive.worker            # run the polling loop
    python -m bdlive.worker --health   # exit 0 when this host's worker heartbeat is fresh

The portal only inserts a 'queued' row (bdlive.runs.request_run); this process claims it,
runs the unchanged workflow, and writes progress/status back to processing_runs. A
heartbeat thread keeps worker_status and the active run's heartbeat_at current, so the
portal can tell a live run from a stale one. If the worker is stopped mid-run, the run is
marked failed on the next start (all stages are idempotent and can simply be re-queued).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time

from bdlive import db, runs

log = logging.getLogger("bdlive.worker")
POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "3"))
HEARTBEAT_SECONDS = float(os.environ.get("WORKER_HEARTBEAT_SECONDS", "10"))
VERSION = "bd-live-worker/1"


class Worker:
    def __init__(self, worker_id: str | None = None):
        self.worker_id = worker_id or os.environ.get("WORKER_ID") or socket.gethostname()
        self.started_at = db.now()
        self.state, self.run_id = "idle", None
        self.stop = threading.Event()
        self._lock = threading.Lock()

    def beat(self):
        with self._lock:
            state, run_id = self.state, self.run_id
        try:
            runs.heartbeat(self.worker_id, state, run_id, self.started_at, VERSION)
        except Exception:  # noqa: BLE001 - a missed beat must not kill the worker
            log.exception("heartbeat failed")

    def _heartbeat_loop(self):
        while not self.stop.wait(HEARTBEAT_SECONDS):
            self.beat()

    def execute(self, run: dict) -> dict:
        from bdlive.workflow import run_workflow

        p = run["params"]
        with self._lock:
            self.state, self.run_id = "running", run["id"]
        self.beat()
        log.info("run %s claimed: %s", run["id"], p)
        try:
            return run_workflow(
                max_jobs=p.get("max_jobs"), sync_existing=bool(p.get("sync_existing")),
                reprocess_existing=bool(p.get("reprocess_existing")), run_id=run["id"],
                job_ids=p.get("job_ids") or None, reanalyse=bool(p.get("reanalyse")),
                progress=lambda msg, frac: log.info("run %s [%3d%%] %s", run["id"],
                                                    int(frac * 100), msg),
            )
        except Exception as exc:  # noqa: BLE001 - run_workflow records its own failures
            log.exception("run %s crashed", run["id"])
            db.update("processing_runs", "id", run["id"], status="failed",
                      finished_at=db.now(), stage="crashed",
                      error=f"worker error: {type(exc).__name__}")
            return {"status": "failed"}
        finally:
            with self._lock:
                self.state, self.run_id = "idle", None
            self.beat()

    def serve(self):
        db.init()
        interrupted = runs.recover_interrupted(self.worker_id)
        if interrupted:
            log.warning("marked interrupted runs as failed: %s", interrupted)
        self.beat()
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="heartbeat").start()
        log.info("worker %s ready (poll %ss)", self.worker_id, POLL_SECONDS)
        while not self.stop.is_set():
            try:
                run = runs.claim_next(self.worker_id)
            except Exception:  # noqa: BLE001 - e.g. transient database lock
                log.exception("claim failed")
                run = None
            if run:
                result = self.execute(run)
                log.info("run %s finished: %s", run["id"], result.get("status"))
                continue
            self.stop.wait(POLL_SECONDS)
        with self._lock:
            self.state = "stopped"
        self.beat()
        log.info("worker stopped")


def health() -> int:
    """Container health: this worker's heartbeat is recent."""
    worker_id = os.environ.get("WORKER_ID") or socket.gethostname()
    row = db.one("SELECT heartbeat_at FROM worker_status WHERE worker_id = ?", (worker_id,))
    age = runs.age_seconds(row["heartbeat_at"]) if row else None
    ok = age is not None and age <= runs.HEARTBEAT_STALE_SECONDS
    print(f"worker {worker_id}: heartbeat age {age if age is None else round(age)}s -> "
          f"{'healthy' if ok else 'unhealthy'}")
    return 0 if ok else 1


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if "--health" in sys.argv:
        sys.exit(health())
    worker = Worker()

    def shutdown(signum, frame):
        log.info("signal %s received; stopping after the current step", signum)
        worker.stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    worker.serve()


if __name__ == "__main__":
    main()
