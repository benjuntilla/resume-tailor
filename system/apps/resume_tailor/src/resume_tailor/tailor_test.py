"""A tailoring run end to end against a scratch resume repo, and its live view."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from resume_tailor import runner, tailor
from resume_tailor.claude_p import ClaudeCLIError
from resume_tailor.conftest import ResumeFixture, git
from resume_tailor.posting import Posting

pytestmark = pytest.mark.timeout(120)

PASTED = "# ML Infrastructure Intern\n\nAcme Robotics wants Python, Spark and Kubernetes. " + "Details. " * 40


def _new_run(url: str | None = None) -> tailor.Run:
    runner.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return tailor.new_run(runner.RUNS_DIR, url)


def _tailor(app_env: ResumeFixture, url: str | None = None, text: str | None = PASTED) -> tailor.Run:
    run = _new_run(url)
    tailor.run_guarded(app_env.repo, run, text)
    return run


# -- the live view ----------------------------------------------------------------


def test_view_merges_progress_lines_into_steps(workspace: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "RUNS_DIR", Path("runs"))
    run = _new_run()
    run.set_step("read", "done", "Read it.")
    run.progress_path.write_text(
        json.dumps({"step": "analyze", "status": "done", "detail": "Wants Python.", "wants": [{"term": "Python", "on_resume": True}]})
        + "\nnot json\n"
        + json.dumps({"step": "bogus", "status": "done"}) + "\n"
        + json.dumps({"step": "skills", "status": "running"}) + "\n"
    )
    steps = {s["key"]: s for s in run.view()["steps"]}
    assert [s["key"] for s in run.view()["steps"]] == [key for key, _ in tailor.STEPS]
    assert steps["read"] == {"key": "read", "title": "Read the job posting", "status": "done", "detail": "Read it.", "changes": []}
    assert steps["analyze"]["wants"] == [{"term": "Python", "on_resume": True}]
    assert steps["skills"]["status"] == "running"
    assert steps["fit"]["status"] == "pending"
    assert "bogus" not in steps


def test_a_stopped_run_leaves_no_step_spinning(workspace: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "RUNS_DIR", Path("runs"))
    run = _new_run()
    run.progress_path.write_text(json.dumps({"step": "skills", "status": "running"}) + "\n")
    run.update(status="failed", error="boom")
    assert {s["key"]: s["status"] for s in run.view()["steps"]}["skills"] == "stopped"


def test_a_finished_run_shows_every_agent_step_done(workspace: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "RUNS_DIR", Path("runs"))
    run = _new_run()
    run.progress_path.write_text(json.dumps({"step": "skills", "status": "running"}) + "\n")
    run.set_step("save", "done", "Saved.")
    run.update(status="done")
    steps = {s["key"]: s for s in run.view()["steps"]}
    assert all(steps[key]["status"] == "done" for key in tailor.AGENT_STEP_KEYS)
    assert steps["skills"]["detail"] == "Done (no notes were recorded for this step)."
    assert steps["save"]["detail"] == "Saved."


def test_the_app_owns_the_final_save_step(workspace: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "RUNS_DIR", Path("runs"))
    run = _new_run()
    run.set_step("save", "done", "Saved by the app.")
    run.progress_path.write_text(json.dumps({"step": "save", "status": "running"}) + "\n")
    assert {s["key"]: s for s in run.view()["steps"]}["save"]["detail"] == "Saved by the app."


# -- dead workers -----------------------------------------------------------------


def test_a_run_whose_worker_is_gone_is_marked_failed(workspace: Path) -> None:
    runs = Path("runs")
    runs.mkdir()
    dead = tailor.new_run(runs, None)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead.update(pid=proc.pid)
    tailor.mark_interrupted(runs)
    assert dead.load()["status"] == "failed"
    assert dead.load()["error"] == "The tailoring stopped unexpectedly. Try again."


def test_a_live_worker_is_left_alone(workspace: Path) -> None:
    runs = Path("runs")
    runs.mkdir()
    run = tailor.new_run(runs, None)
    run.update(status="queued")
    live = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()", run.id], stdin=subprocess.PIPE)
    try:
        run.update(pid=live.pid)
        tailor.mark_interrupted(runs)
        assert run.load()["status"] == "queued"
    finally:
        live.kill()
        live.wait()
    # A pid reused by an unrelated process does not count as the worker.
    run.update(pid=os.getpid())
    tailor.mark_interrupted(runs)
    assert run.load()["status"] == "failed"


def test_a_worker_still_starting_gets_a_grace_period(workspace: Path) -> None:
    runs = Path("runs")
    runs.mkdir()
    run = tailor.new_run(runs, None)
    tailor.mark_interrupted(runs)
    assert run.load()["status"] == "running"
    old = time.time() - tailor.WORKER_START_GRACE_SECONDS - 5
    os.utime(run.record_path, (old, old))
    tailor.mark_interrupted(runs)
    assert run.load()["status"] == "failed"


def test_finished_runs_are_not_touched(workspace: Path) -> None:
    runs = Path("runs")
    runs.mkdir()
    run = tailor.new_run(runs, None)
    run.update(status="done")
    old = time.time() - 3600
    os.utime(run.record_path, (old, old))
    tailor.mark_interrupted(runs)
    assert run.load()["status"] == "done"
    assert tailor.list_runs(Path("missing")) == []


# -- naming -----------------------------------------------------------------------


def test_the_job_is_named_by_the_completion(app_env: ResumeFixture) -> None:
    posting = Posting(url=None, markdown=PASTED, raw_html="")
    assert tailor._name_job(posting) == ("Acme Robotics", "ML Infra Intern", "acme-ml-infra-intern")


def test_an_unhelpful_naming_reply_falls_back_to_the_posting(app_env: ResumeFixture, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_NAMING_REPLY", "I cannot tell.")
    posting = Posting(url=None, markdown=PASTED, raw_html="", title="Data Engineer", company="Globex & Co")
    assert tailor._name_job(posting) == ("Globex & Co", "Data Engineer", "globex-co-data-engineer")


def test_a_failed_naming_call_raises(app_env: ResumeFixture, monkeypatch) -> None:
    monkeypatch.setattr(tailor, "CLAUDE_COMMAND", "/bin/false")
    with pytest.raises(ClaudeCLIError):
        tailor._name_job(Posting(url=None, markdown=PASTED, raw_html=""))


def test_slugify() -> None:
    assert tailor._slugify("  Stripe -- ML Infra (New Grad)! ") == "stripe-ml-infra-new-grad"
    assert len(tailor._slugify("x" * 100)) == 60


# -- whole runs -------------------------------------------------------------------


def test_a_run_tailors_on_its_own_branch_and_pushes(app_env: ResumeFixture) -> None:
    main_before = app_env.main_sha()
    run = _tailor(app_env)
    record = run.load()
    assert record["status"] == "done", record["error"]
    assert record["slug"] == "acme-ml-infra-intern"
    assert record["branch"] == "tailor/acme-ml-infra-intern"
    assert (record["company"], record["role"], record["cost_usd"]) == ("Acme Robotics", "ML Infra Intern", 0.42)
    # main is never touched, locally or on origin.
    assert app_env.main_sha() == main_before == app_env.origin_main_sha()
    branch = "tailor/acme-ml-infra-intern"
    assert git(app_env.origin, "rev-parse", f"refs/heads/{branch}") == git(app_env.path, "rev-parse", branch)
    files = git(app_env.path, "ls-tree", "-r", "--name-only", branch).split()
    assert {"base.yaml", "Alex_Doe_resume.pdf", "job/meta.json", "job/posting.md", "job/changes.md"} <= set(files)
    assert not any(f.startswith("data/") for f in files)
    posting = git(app_env.path, "show", f"{branch}:job/posting.md")
    assert posting.startswith("Source: pasted by the user\n\n# ML Infrastructure Intern")
    # The raw records stay with the run.
    assert (run.dir / "posting.md").read_text() == PASTED.strip()
    assert '"type": "result"' in (run.dir / "transcript.jsonl").read_text()
    steps = {s["key"]: s for s in run.view()["steps"]}
    assert all(s["status"] == "done" for s in steps.values())
    assert steps["read"]["detail"] == "Read the full description from the description you pasted."
    assert steps["save"]["detail"].endswith("and uploaded it to GitHub.")
    assert steps["experience"]["changes"][0]["why"] == "ownership"


def test_a_second_run_for_the_same_job_gets_its_own_branch(app_env: ResumeFixture) -> None:
    _tailor(app_env)
    run = _tailor(app_env)
    assert run.load()["branch"] == "tailor/acme-ml-infra-intern-2"


def test_notes_written_inside_the_job_folder_are_moved_back(app_env: ResumeFixture, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "relative_progress")
    run = _tailor(app_env)
    assert run.load()["status"] == "done", run.load()["error"]
    files = git(app_env.path, "ls-tree", "-r", "--name-only", "tailor/acme-ml-infra-intern").split()
    assert not any(f.startswith("data/") for f in files)
    assert not (app_env.repo.worktree_path("acme-ml-infra-intern") / "data").exists()
    assert '"step": "fit"' in run.progress_path.read_text()
    assert {s["key"]: s for s in run.view()["steps"]}["fit"]["detail"] == "Fake fit step."


def test_an_agent_error_fails_the_run_with_its_message(app_env: ResumeFixture, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "error")
    run = _tailor(app_env)
    record = run.load()
    assert record["status"] == "failed"
    assert "fake agent crashed" in record["error"]
    assert all(s["status"] != "running" for s in run.view()["steps"])
    assert app_env.main_sha() == app_env.origin_main_sha()


def test_an_agent_that_saves_nothing_fails_the_run(app_env: ResumeFixture, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "nochange")
    record = _tailor(app_env).load()
    assert (record["status"], record["error"]) == ("failed", "The tailoring finished without changing the resume.")


def test_a_silent_agent_is_stopped_at_the_time_limit(app_env: ResumeFixture, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setattr(tailor, "AGENT_TIMEOUT_SECONDS", 2)
    started = time.monotonic()
    record = _tailor(app_env).load()
    assert time.monotonic() - started < 60
    assert record["status"] == "failed"
    assert record["error"].startswith("The tailoring took longer than")


def test_an_upload_failure_still_saves_the_version(app_env: ResumeFixture) -> None:
    run = _new_run()
    # Fetch succeeds, the push is refused.
    hook = app_env.origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
    hook.chmod(0o755)
    tailor.run_guarded(app_env.repo, run, PASTED)
    record = run.load()
    assert record["status"] == "done", record["error"]
    assert "Uploading to GitHub failed" in {s["key"]: s for s in run.view()["steps"]}["save"]["detail"]
    assert git(app_env.path, "rev-parse", "--verify", "-q", "refs/heads/tailor/acme-ml-infra-intern")


def test_an_unreadable_link_asks_for_a_paste(app_env: ResumeFixture) -> None:
    run = _tailor(app_env, url="http://127.0.0.1:1/job", text=None)
    record = run.load()
    assert record["status"] == "needs_paste"
    assert record["error"].startswith("Couldn't open that link")
    assert {s["key"]: s["status"] for s in run.view()["steps"]}["read"] == "needs_paste"
    assert record["slug"] is None
