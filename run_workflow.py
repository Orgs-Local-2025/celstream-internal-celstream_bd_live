"""CLI: run the full live workflow once.  Usage: python run_workflow.py [max_jobs]"""

import json
import logging
import sys

from bdlive.workflow import run_workflow

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    max_jobs = int(args[0]) if args else None
    result = run_workflow(max_jobs, sync_existing="--sync-existing" in sys.argv,
                          reprocess_existing="--reprocess-existing" in sys.argv)
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result.get("status") == "success" else 1)
