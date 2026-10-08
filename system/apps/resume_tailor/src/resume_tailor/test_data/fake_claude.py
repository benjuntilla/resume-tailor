"""Stand-in for the ``claude`` binary in tests.

Two shapes, told apart by ``--output-format``:

- ``json``: the job-naming completion. Replies with the company/role JSON the app
  asks for (``FAKE_NAMING_REPLY`` overrides the text).
- ``stream-json``: the tailoring agent, run inside the job's worktree. It writes
  progress lines to the path named in the prompt, replaces ``base.yaml`` with
  ``FAKE_TAILORED``, renders with ``FAKE_RENDER``, writes the job record and
  commits, then prints a stream-json result line.

``FAKE_MODE`` picks a failure: ``error`` (exits 1), ``nochange`` (commits
nothing), ``relative_progress`` (writes its notes under a relative path, as an
agent that resolved the path against its own folder would), ``hang`` (goes
silent). ``FAKE_DELAY`` is the seconds to wait inside each step.
"""

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path


def _wait(seconds: float) -> None:
    threading.Event().wait(seconds)


def _naming() -> int:
    reply = os.environ.get(
        "FAKE_NAMING_REPLY", '{"company": "Acme Robotics", "role": "ML Infra Intern", "slug": "acme-ml-infra-intern"}'
    )
    sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": reply,
                                 "total_cost_usd": 0.001}))
    return 0


def _agent(prompt: str) -> int:
    mode = os.environ.get("FAKE_MODE", "ok")
    delay = float(os.environ.get("FAKE_DELAY", "0"))
    progress = Path(re.search(r"append one JSON object on its own line to (\S+progress\.jsonl)", prompt).group(1))
    if mode == "relative_progress":
        progress = Path(str(progress).split(os.environ["FAKE_WORKSPACE"] + "/", 1)[1])
        progress.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout.write(json.dumps({"type": "system", "subtype": "init"}) + "\n")
    sys.stdout.flush()

    def emit(**event: object) -> None:
        with progress.open("a") as out:
            out.write(json.dumps(event) + "\n")

    if mode == "hang":
        _wait(600)
        return 0
    if mode == "error":
        emit(step="analyze", status="running")
        sys.stderr.write("fake agent crashed\n")
        return 1
    for key in ["analyze", "skills", "experience", "projects", "coursework", "keywords", "fit"]:
        emit(step=key, status="running")
        _wait(delay)
        extra = {"wants": [{"term": "Python", "on_resume": True}, {"term": "Spark", "on_resume": False}]} if key == "analyze" else {}
        changes = [{"before": "Owned reliability by interviewing users", "after": "Owned reliability end-to-end", "why": "ownership"}] if key == "experience" else []
        emit(step=key, status="done", detail=f"Fake {key} step.", changes=changes, **extra)
    if mode != "nochange":
        Path("base.yaml").write_text(Path(os.environ["FAKE_TAILORED"]).read_text())
        subprocess.run([sys.executable, os.environ["FAKE_RENDER"], "base.yaml"], check=True)
        base_commit = re.search(r"created from main at ([0-9a-f]{40})", prompt).group(1)
        Path("job/meta.json").write_text(json.dumps({"company": "Acme Robotics", "role": "ML Infra Intern",
                                                     "url": None, "tailored_on": "2026-10-08", "base_commit": base_commit}))
        Path("job/changes.md").write_text("## Experience\n\n- Reworded the reliability bullet.\n")
        subprocess.run("git add -A && git commit -qm 'Tailor resume'", shell=True, check=True)
        emit(step="save", status="done", detail="Fake save.", changes=[])
    sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.42,
                                 "result": "ok"}) + "\n")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    output_format = argv[argv.index("--output-format") + 1]
    if output_format == "json":
        return _naming()
    return _agent(argv[argv.index("-p") + 1])


if __name__ == "__main__":
    sys.exit(main())
