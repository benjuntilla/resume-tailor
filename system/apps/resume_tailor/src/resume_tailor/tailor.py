"""One tailoring run: read the posting, branch, let Claude follow the repo's skill, push.

The app does the deterministic parts itself (reading the link, creating the
branch + worktree, pushing) and hands the judgement parts to a headless Claude
Code agent running inside the job's worktree, where the repo's own
``tailor-resume`` skill lives. The agent reports progress by appending JSON lines
to a file the app reads; its full event stream is kept beside the run as the raw
record.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from resume_tailor.claude_p import ClaudeCLIError, _child_env, claude_p_completion
from resume_tailor.posting import Posting, PostingUnreadable, read_posting
from resume_tailor.repo import BRANCH_PREFIX, GitError, ResumeRepo

# The agent that does the tailoring, and the quick model that names the job.
TAILOR_MODEL = "claude-opus-5-5"
NAMING_MODEL = "claude-haiku-4-5"
AGENT_TIMEOUT_SECONDS = 30 * 60
# The Claude Code binary the tailoring agent runs as; tests point it at a stand-in.
CLAUDE_COMMAND = os.environ.get("RESUME_TAILOR_CLAUDE", "claude")

STEPS: list[tuple[str, str]] = [
    ("read", "Read the job posting"),
    ("setup", "Set up a separate copy for this job"),
    ("analyze", "Work out what the job wants"),
    ("skills", "Reorder skills"),
    ("experience", "Reword experience bullets"),
    ("projects", "Swap projects"),
    ("coursework", "Update coursework"),
    ("keywords", "Update bolded words"),
    ("fit", "Build and check it fits one page"),
    ("save", "Save this version"),
]
AGENT_STEP_KEYS = ["analyze", "skills", "experience", "projects", "coursework", "keywords", "fit", "save"]


class TailorError(RuntimeError):
    """A run stopped for a reason the user is shown as is."""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class Run:
    """A run's on-disk record under ``<runs_dir>/<id>/``."""

    def __init__(self, runs_dir: Path, run_id: str) -> None:
        self.id = run_id
        self.dir = runs_dir / run_id

    @property
    def record_path(self) -> Path:
        return self.dir / "run.json"

    @property
    def progress_path(self) -> Path:
        return self.dir / "progress.jsonl"

    def load(self) -> dict:
        return json.loads(self.record_path.read_text())

    def save(self, record: dict) -> None:
        tmp = self.record_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2))
        tmp.replace(self.record_path)

    def update(self, **fields) -> dict:
        record = self.load()
        record.update(fields)
        self.save(record)
        return record

    def set_step(self, key: str, status: str, detail: str = "", changes: list | None = None) -> None:
        record = self.load()
        record["steps"][key] = {"status": status, "detail": detail, "changes": changes or []}
        self.save(record)

    def view(self) -> dict:
        """The record with the agent's progress lines merged into its steps."""
        record = self.load()
        steps = {key: dict(value) for key, value in record["steps"].items()}
        if self.progress_path.exists():
            for line in self.progress_path.read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                key = event.get("step")
                if key not in AGENT_STEP_KEYS:
                    continue
                if key == "save" and steps.get("save", {}).get("status") in ("done", "failed"):
                    continue  # the app owns the final state of the save step
                current = steps.setdefault(key, {"status": "pending"})
                current["status"] = event.get("status", current.get("status"))
                for field in ("detail", "changes", "wants"):
                    if field in event:
                        current[field] = event[field]
        # A run that stopped leaves no step spinning; a finished one leaves none unfinished.
        if record["status"] in ("failed", "needs_paste"):
            for value in steps.values():
                if value.get("status") == "running":
                    value["status"] = "stopped"
        if record["status"] == "done":
            for key in AGENT_STEP_KEYS:
                value = steps.setdefault(key, {"status": "pending"})
                if value.get("status") != "done":
                    value["status"] = "done"
                    value.setdefault("detail", "")
                    value["detail"] = value.get("detail") or "Done (no notes were recorded for this step)."
        record["steps"] = [
            {"key": key, "title": title, **steps.get(key, {"status": "pending"})} for key, title in STEPS
        ]
        return record


def new_run(runs_dir: Path, url: str | None) -> Run:
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    run = Run(runs_dir, run_id)
    run.dir.mkdir(parents=True)
    run.save({
        "id": run_id,
        "url": url,
        "created": _now(),
        "status": "running",
        "error": None,
        "slug": None,
        "branch": None,
        "company": None,
        "role": None,
        "cost_usd": None,
        "steps": {},
    })
    return run


def list_runs(runs_dir: Path) -> list[Run]:
    if not runs_dir.exists():
        return []
    return [Run(runs_dir, p.name) for p in sorted(runs_dir.iterdir(), reverse=True) if (p / "run.json").exists()]


def _alive(pid: int | None, run_id: str) -> bool:
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return False
    return run_id in cmdline


WORKER_START_GRACE_SECONDS = 60


def mark_interrupted(runs_dir: Path) -> None:
    """Any run still marked active whose worker process is gone died with it."""
    for run in list_runs(runs_dir):
        record = run.load()
        if record["status"] not in ("running", "queued"):
            continue
        if not record.get("pid") and time.time() - run.record_path.stat().st_mtime < WORKER_START_GRACE_SECONDS:
            continue  # the worker hasn't recorded itself yet
        if not _alive(record.get("pid"), run.id):
            run.update(status="failed", error="The tailoring stopped unexpectedly. Try again.")


# -- the run itself -----------------------------------------------------------


def start(repo: ResumeRepo, run: Run, pasted_text: str | None = None) -> None:
    """Start the run in its own worker process (see ``resume_tailor.worker``)."""
    if pasted_text:
        (run.dir / "pasted.md").write_text(pasted_text)
    log = (run.dir / "worker.log").open("a")
    proc = subprocess.Popen(
        [sys.executable, "-m", "resume_tailor.worker", str(run.dir.parent.absolute()), run.id, str(repo.path)],
        cwd=os.getcwd(), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
    )
    log.close()
    # Reap it if it finishes while this process is still around; after a restart
    # the worker is reparented and reaped by init.
    threading.Thread(target=proc.wait, daemon=True).start()


def run_guarded(repo: ResumeRepo, run: Run, pasted_text: str | None) -> None:
    try:
        _run(repo, run, pasted_text)
    except (TailorError, GitError, ClaudeCLIError, OSError, ValueError, subprocess.SubprocessError) as exc:
        record = run.load()
        for key, value in record["steps"].items():
            if value.get("status") == "running":
                record["steps"][key]["status"] = "failed"
        record.update(status="failed", error=str(exc))
        run.save(record)


def _run(repo: ResumeRepo, run: Run, pasted_text: str | None) -> None:
    record = run.load()
    url = record["url"]

    # 1. Read the posting.
    run.set_step("read", "running")
    if pasted_text:
        posting = Posting(url=url, markdown=pasted_text.strip(), raw_html="")
        source = "the description you pasted"
    else:
        try:
            posting = read_posting(url)
        except PostingUnreadable as exc:
            run.set_step("read", "needs_paste", str(exc))
            run.update(status="needs_paste", error=str(exc))
            return
        source = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    if posting.raw_html:
        (run.dir / "posting.html").write_text(posting.raw_html)
    (run.dir / "posting.md").write_text(posting.markdown)
    run.set_step("read", "done", f"Read the full description from {source}.")

    # 2. Name the job and set up its branch + worktree.
    run.set_step("setup", "running")
    company, role, slug = _name_job(posting)
    repo.fetch()
    slug = repo.free_slug(slug)
    branch = BRANCH_PREFIX + slug
    worktree = repo.create_worktree(slug)
    base_commit = repo.git("rev-parse", "origin/main").strip()
    job_dir = worktree / "job"
    job_dir.mkdir(exist_ok=True)
    header = f"Source: {url}\n\n" if url else "Source: pasted by the user\n\n"
    (job_dir / "posting.md").write_text(header + posting.markdown + "\n")
    run.update(slug=slug, branch=branch, company=company, role=role, base_commit=base_commit)
    run.set_step("setup", "done", f"New branch {branch} from main, in its own folder. Your main resume is untouched.")

    # 3-9. The agent follows the repo's skill inside the worktree.
    run.set_step("analyze", "running")
    prompt = _agent_prompt(company, role, url, branch, base_commit, run.progress_path.absolute())
    cost = _run_agent(prompt, worktree, run)
    run.update(cost_usd=cost)

    _recover_progress(run, worktree)

    # 10. Make sure it's committed, then push.
    if repo.git("status", "--porcelain", cwd=worktree).strip():
        repo.git("add", "-A", cwd=worktree)
        repo.git("commit", "-q", "-m", f"Tailor resume for {company} {role}", cwd=worktree)
    # The app's own job/posting.md is always a change, so judge by the resume itself.
    if not repo.git("diff", "--name-only", base_commit, branch, "--", "base.yaml").strip():
        raise TailorError("The tailoring finished without changing the resume.")
    pushed_note = ""
    try:
        repo.push(branch)
        pushed_note = " and uploaded it to GitHub"
    except GitError as exc:  # the version is saved locally either way
        pushed_note = f". Uploading to GitHub failed ({exc})"
    run.set_step("save", "done", f"Saved the resume, the job posting and the change log together{pushed_note}.")
    run.update(status="done", finished=_now())


def _recover_progress(run: Run, worktree: Path) -> None:
    """Move a progress file the agent wrote inside the worktree back to the run.

    The prompt names an absolute path, but if the agent resolved it against its
    own working directory the notes land in the job's copy, where the view never
    sees them and the commit would carry them into the resume branch.
    """
    try:
        relative = run.progress_path.absolute().relative_to(Path.cwd())
    except ValueError:
        return
    stray = worktree / relative
    if not stray.exists():
        return
    with run.progress_path.open("a") as out:
        out.write(stray.read_text())
    top = worktree / relative.parts[0]
    subprocess.run(["git", "rm", "-r", "-q", "--cached", "--ignore-unmatch", relative.parts[0]], cwd=worktree, check=False)
    shutil.rmtree(top, ignore_errors=True)


def _slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")[:60]


def _name_job(posting: Posting) -> tuple[str, str, str]:
    result = claude_p_completion(
        "Job posting:\n\n" + posting.markdown[:6000],
        system=(
            "Identify the hiring company and the role from a job posting. Reply with only a JSON object: "
            '{"company": "<company name as commonly written>", "role": "<role title, short>", '
            '"slug": "<company>-<role> in lowercase words joined by hyphens, at most 5 words, '
            'e.g. stripe-ml-infra-new-grad or imc-swe-intern>"}'
        ),
        model=NAMING_MODEL,
        strip_mngr_agent_vars=True,
        binary=CLAUDE_COMMAND,
    )
    match = re.search(r"\{.*\}", result.text, re.S)
    data = json.loads(match.group(0)) if match else {}
    company = data.get("company") or posting.company or "Company"
    role = data.get("role") or posting.title or "Role"
    slug = _slugify(data.get("slug") or f"{company}-{role}") or "job"
    return company, role, slug


def _agent_prompt(company: str, role: str, url: str | None, branch: str, base_commit: str, progress: Path) -> str:
    today = dt.date.today().isoformat()
    return f"""Tailor this resume to the job posting in job/posting.md, following the tailor-resume skill at .claude/skills/tailor-resume/SKILL.md. Read the skill first and follow it exactly.

Already done for you, so skip it: step 0. You are already inside this job's worktree, on branch {branch}, created from main at {base_commit}, and job/posting.md is written. Do not create branches or worktrees, do not fetch, and do not push. The app uploads the branch after you finish.

Job details: company {company!r}, role {role!r}, posting link {url or "none (the user pasted the description)"}, today's date {today}.

Render with: uvx 'rendercv[full]@2.8' render base.yaml
Check the page by reading the PDF with the Read tool after each render.

PROGRESS REPORTING (required). The user is watching a live view of your work. At the start and end of each step, append one JSON object on its own line to {progress}, using exactly this command shape:

cat >> {progress} <<'JSON'
{{"step": "skills", "status": "running"}}
JSON

The step keys, in order: analyze, skills, experience, projects, coursework, keywords, fit, save.
When a step finishes, write:
{{"step": "<key>", "status": "done", "detail": "<one plain-English sentence on what you did>", "changes": [{{"before": "<old text, or empty if added>", "after": "<new text, or empty if removed>", "why": "<short reason tied to the posting>"}}]}}
- before/after are the human-readable line (a bullet, a skills row, a project name), never YAML.
- A step that needed no change still gets a done line with an empty changes list and a detail saying why.
- For analyze, also include "wants": [{{"term": "<skill or theme>", "on_resume": true|false}}] with the 8 to 15 most important things the posting asks for.
- For fit, each render that needed a fix is one change (before: what was wrong, after: what you did).

For the save step: write job/meta.json ({{"company", "role", "url", "tailored_on", "base_commit"}} as the skill says), write job/changes.md, then run: git add -A && git commit -m "Tailor resume for {company} {role}". Write the save step's done line last, after the commit.
"""


def _run_agent(prompt: str, worktree: Path, run: Run) -> float | None:
    argv = [
        CLAUDE_COMMAND, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--model", TAILOR_MODEL,
        "--permission-mode", "bypassPermissions",
    ]
    env = _child_env(strip_mngr_agent_vars=True)
    cost: float | None = None
    result_error: str | None = None
    with (run.dir / "transcript.jsonl").open("w") as transcript, (run.dir / "agent-stderr.log").open("w") as stderr:
        proc = subprocess.Popen(argv, cwd=worktree, env=env, stdout=subprocess.PIPE, stderr=stderr, text=True)
        # A watchdog rather than a check between lines, so an agent that goes
        # silent is stopped too.
        watchdog = threading.Timer(AGENT_TIMEOUT_SECONDS, proc.kill)
        watchdog.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                transcript.write(line)
                transcript.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    cost = event.get("total_cost_usd")
                    if event.get("is_error") or event.get("subtype") != "success":
                        result_error = str(event.get("result") or event.get("subtype"))
            proc.wait()
        finally:
            timed_out = watchdog.finished.is_set() and proc.returncode != 0
            watchdog.cancel()
    if timed_out:
        raise TailorError(f"The tailoring took longer than {AGENT_TIMEOUT_SECONDS // 60} minutes and was stopped.")
    if proc.returncode != 0 or result_error:
        tail = (run.dir / "agent-stderr.log").read_text()[-300:]
        raise TailorError(f"The tailoring agent stopped with an error: {result_error or tail or proc.returncode}")
    return cost
