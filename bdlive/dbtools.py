"""Database maintenance for the Docker deployment (uses the SQLite online-backup API, so it is
safe while the portal/worker are running).

    python -m bdlive.dbtools status
    python -m bdlive.dbtools backup [label]
    python -m bdlive.dbtools import <source.db> [--force]   # seed the volume from a local DB
    python -m bdlive.dbtools restore <backup.db> --force    # stop the worker first
    python -m bdlive.dbtools list
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from bdlive import db
from bdlive.settings import settings

TABLES = ["jobs", "companies", "contacts", "capability_analysis", "scoring_results",
          "integration_logs", "processing_runs"]


def counts(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in TABLES if t in existing}
        result["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        result["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        return result
    finally:
        conn.close()


def staged_copy(source: Path) -> Path:
    """Copy the source DB (plus any un-checkpointed -wal file) to a writable temp dir.
    A WAL database on a read-only mount cannot be opened in place (SQLite must create its
    -shm file), and ignoring the -wal file would silently drop the newest changes."""
    import shutil
    import tempfile

    stage = Path(tempfile.mkdtemp(prefix="bd-import-")) / "source.db"
    shutil.copy2(source, stage)
    wal = Path(str(source) + "-wal")
    if wal.exists():
        shutil.copy2(wal, Path(str(stage) + "-wal"))
    conn = sqlite3.connect(stage)  # opening applies the WAL; checkpoint folds it in
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return stage


def copy_into_live(source: Path, force: bool) -> dict:
    target = settings().database_path
    if not source.exists():
        raise SystemExit(f"source not found: {source}")
    source = staged_copy(source)
    if counts(source).get("integrity") != "ok":
        raise SystemExit("source failed PRAGMA integrity_check; refusing to import")
    if target.exists() and counts(target).get("jobs", 0) and not force:
        raise SystemExit(f"target {target} already contains data; pass --force to replace it "
                         "(a backup is taken first)")
    if target.exists():
        print("pre-import backup:", db.backup(label="pre-import"))
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    db.init()  # apply any pending migrations (with their own backup)
    return counts(target)


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "status"
    force = "--force" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    if command == "status":
        db.init()
        print(json.dumps({"database": str(settings().database_path),
                          **counts(settings().database_path)}, indent=1))
    elif command == "backup":
        db.init()
        path = db.backup(label=args[0] if args else "manual")
        print(json.dumps({"backup": str(path), **counts(path)}, indent=1))
    elif command in ("import", "restore"):
        if not args:
            raise SystemExit(f"usage: dbtools {command} <path.db>")
        if command == "restore" and not force:
            raise SystemExit("restore replaces the live database; stop bd-live-worker and pass "
                             "--force")
        print(json.dumps({"imported_from": args[0], **copy_into_live(Path(args[0]), force)},
                         indent=1))
    elif command == "list":
        folder = settings().database_path.parent / "backups"
        import os

        folder = Path(os.environ.get("LIVE_BACKUP_DIR") or folder)
        for path in sorted(folder.glob("*.db")):
            print(path.name, path.stat().st_size)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
