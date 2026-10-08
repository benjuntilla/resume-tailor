"""Run one tailoring job in its own process, so it outlives app restarts.

The app starts ``python -m resume_tailor.worker <runs_dir> <run_id> <repo>`` in a
new session (outside supervisord's process group), records its pid on the run,
and only ever reads the run's files afterwards. One job runs at a time: a worker
that finds another holding the lock marks its run ``queued`` and waits.
"""

from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path

from resume_tailor import tailor
from resume_tailor.repo import ResumeRepo

LOCK_NAME = ".worker.lock"


def main() -> None:
    runs_dir, run_id, repo_path = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    run = tailor.Run(runs_dir, run_id)
    # The worker is the only writer of its run's record while it lives.
    run.update(pid=os.getpid())
    pasted = run.dir / "pasted.md"
    pasted_text = pasted.read_text() if pasted.exists() else None
    with (runs_dir / LOCK_NAME).open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            run.update(status="queued")
            fcntl.flock(lock, fcntl.LOCK_EX)
            run.update(status="running")
        tailor.run_guarded(ResumeRepo(repo_path), run, pasted_text)


if __name__ == "__main__":
    main()
