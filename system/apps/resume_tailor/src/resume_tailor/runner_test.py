"""The app's routes, with tailoring runs in real worker processes against a scratch repo."""

from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from resume_tailor import runner, tailor
from resume_tailor.conftest import ResumeFixture, git

pytestmark = pytest.mark.timeout(180)

PASTED = "# ML Infrastructure Intern\n\nAcme Robotics wants Python, Spark and Kubernetes. " + "Details. " * 40


@pytest.fixture
def client(app_env: ResumeFixture):
    return runner.app.test_client()


def _wait_for(client, key: str, statuses: tuple[str, ...], timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    job = client.get(f"/api/job/{key}").get_json()
    while not (job and job["run"] and job["run"]["status"] in statuses):
        if time.monotonic() > deadline:
            raise AssertionError(f"run never reached {statuses}: {job and job['run']}")
        threading.Event().wait(0.2)
        job = client.get(f"/api/job/{key}").get_json()
    return job


def _start(client, **body) -> str:
    response = client.post("/api/tailor", json=body)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["key"]


def test_the_page_is_served_with_the_shell_script(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    page = response.get_data(as_text=True)
    assert "<title>Resume Tailor</title>" in page
    assert 'import { connectToShell } from "/_static/app_contract.js";' in page
    assert page.index("connectToShell") < page.index("</body>")


def test_health_and_static_modules(client) -> None:
    assert client.get("/health").get_json() == {"status": "ok"}
    assert client.get("/_static/../../etc/passwd").status_code == 404
    assert client.get("/_static/other.js").status_code == 404


def test_an_empty_request_is_refused(client) -> None:
    response = client.post("/api/tailor", json={"url": "  ", "text": ""})
    assert response.status_code == 400
    assert response.get_json()["error"] == "Paste a job link first."


def test_no_jobs_yet(client) -> None:
    assert client.get("/api/jobs").get_json() == {"items": [], "github": None}
    assert client.get("/api/job/nothing-here").status_code == 404


def test_tailoring_runs_in_a_worker_and_the_result_is_reviewable(client, app_env: ResumeFixture) -> None:
    key = _start(client, text=PASTED)
    assert key.startswith("run-")
    job = _wait_for(client, key, ("done", "failed"))
    assert job["run"]["status"] == "done", job["run"]["error"]
    assert job["run"]["pid"] != os.getpid()
    assert job["branch"] == "tailor/acme-ml-infra-intern"
    assert job["has_branch"] and job["has_pdf"]
    assert job["company"] == "Acme Robotics" and job["role"] == "ML Infra Intern"
    assert job["resume_before"] != job["resume_after"]
    assert "Owned reliability end-to-end" in job["diff"]
    assert len(job["changes"]) == 7 and job["undone"] == []
    assert job["posting_html"].startswith("<p>Source: pasted by the user</p>\n<h1>ML Infrastructure Intern</h1>")
    assert "Reworded the reliability bullet." in job["changes_html"]

    # The sidebar lists it once, by its branch.
    [item] = client.get("/api/jobs").get_json()["items"]
    assert (item["key"], item["company"], item["status"]) == ("acme-ml-infra-intern", "Acme Robotics", "done")
    by_slug = client.get("/api/job/acme-ml-infra-intern").get_json()
    assert by_slug["run"]["id"] == job["run"]["id"]

    tailored = client.get("/api/job/acme-ml-infra-intern/tailored.pdf")
    assert tailored.data.startswith(b"%PDF") and tailored.mimetype == "application/pdf"
    original = client.get("/api/job/acme-ml-infra-intern/original.pdf?download=1")
    assert original.data.startswith(b"%PDF") and original.data != tailored.data
    assert "attachment; filename=resume-original.pdf" in original.headers["Content-Disposition"]
    assert client.get("/api/job/acme-ml-infra-intern/other.pdf").status_code == 404

    preview = client.get("/api/job/acme-ml-infra-intern/tailored.png")
    assert preview.data.startswith(b"\x89PNG") and preview.headers["X-Page-Count"] == "1"
    assert (runner.PREVIEWS_DIR / ".nobackup").exists()


def test_a_job_on_origin_only_is_listed_and_viewable(client, app_env: ResumeFixture) -> None:
    app_env.make_tailored_branch("globex-data-intern")
    git(app_env.path, "push", "-q", "origin", "tailor/globex-data-intern")
    git(app_env.path, "worktree", "remove", "--force", str(app_env.repo.worktree_path("globex-data-intern")))
    git(app_env.path, "branch", "-q", "-D", "tailor/globex-data-intern")
    git(app_env.path, "update-ref", "-d", "refs/remotes/origin/tailor/globex-data-intern")
    assert client.get("/api/jobs").get_json()["items"] == []
    assert client.post("/api/refresh").get_json() == {"ok": True}
    [item] = client.get("/api/jobs").get_json()["items"]
    assert (item["key"], item["company"], item["role"], item["date"]) == ("globex-data-intern", "Globex", "Data Intern", "2026-10-08")
    job = client.get("/api/job/globex-data-intern").get_json()
    assert job["run"] is None and job["url"] == "https://jobs.example.com/1"
    # The posting comes from a web page: it is shown escaped.
    assert "&lt;script&gt;" in job["posting_html"] and "<script>" not in job["posting_html"]


def test_undo_and_restore_through_the_api(client, app_env: ResumeFixture) -> None:
    app_env.make_tailored_branch("globex-data-intern")
    job = client.get("/api/job/globex-data-intern").get_json()
    [globex] = [c for c in job["changes"] if c["label"] == "Globex"]
    assert client.post(f"/api/job/globex-data-intern/changes/{globex['id']}/undo").get_json() == {"ok": True}
    job = client.get("/api/job/globex-data-intern").get_json()
    assert [u["label"] for u in job["undone"]] == ["Globex"]
    assert "      # - company: Globex" in job["resume_after"]
    again = client.post(f"/api/job/globex-data-intern/changes/{globex['id']}/undo")
    assert again.status_code == 409 and "no longer in this version" in again.get_json()["error"]
    commit = job["undone"][0]["commit"]
    assert client.post(f"/api/job/globex-data-intern/undone/{commit}/restore").get_json() == {"ok": True}
    job = client.get("/api/job/globex-data-intern").get_json()
    assert job["undone"] == [] and globex["id"] in {c["id"] for c in job["changes"]}


def test_review_waits_for_a_running_tailoring(client, app_env: ResumeFixture, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_DELAY", "1")
    key = _start(client, text=PASTED)
    job = _wait_for(client, key, ("running",))
    while not job["run"].get("slug"):
        job = client.get(f"/api/job/{key}").get_json()
    response = client.post(f"/api/job/{job['run']['slug']}/changes/abc/undo")
    assert response.status_code == 409
    assert response.get_json()["error"] == "Wait for the tailoring to finish first."
    assert _wait_for(client, key, ("done", "failed"))["run"]["status"] == "done"


def test_a_second_job_queues_behind_the_first(client, app_env: ResumeFixture, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_DELAY", "0.7")
    first = _start(client, text=PASTED)
    _wait_for(client, first, ("running",))
    while not client.get(f"/api/job/{first}").get_json()["run"].get("slug"):
        threading.Event().wait(0.1)
    second = _start(client, text=PASTED)
    assert _wait_for(client, second, ("queued",))["run"]["status"] == "queued"
    assert client.get(f"/api/job/{first}").get_json()["run"]["status"] == "running"
    statuses = {i["status"] for i in client.get("/api/jobs").get_json()["items"]}
    assert statuses == {"running", "queued"}
    assert _wait_for(client, first, ("done", "failed"))["run"]["status"] == "done"
    done = _wait_for(client, second, ("done", "failed"))
    assert done["run"]["status"] == "done" and done["branch"] == "tailor/acme-ml-infra-intern-2"


def test_a_killed_worker_shows_as_stopped(client, app_env: ResumeFixture, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    key = _start(client, text=PASTED)
    job = _wait_for(client, key, ("running",))
    while not job["run"].get("pid"):
        job = client.get(f"/api/job/{key}").get_json()
    os.killpg(job["run"]["pid"], signal.SIGKILL)
    deadline = time.monotonic() + 30
    while tailor._alive(job["run"]["pid"], job["run"]["id"]) and time.monotonic() < deadline:
        threading.Event().wait(0.1)
    [item] = client.get("/api/jobs").get_json()["items"]  # listing jobs notices the dead worker
    assert item["status"] == "failed"
    run = client.get(f"/api/job/{key}").get_json()["run"]
    assert run["error"] == "The tailoring stopped unexpectedly. Try again."
    assert all(s["status"] != "running" for s in run["steps"])


def test_a_link_that_cannot_be_read_asks_for_the_text(client, app_env: ResumeFixture) -> None:
    key = _start(client, url="127.0.0.1:1/job")
    job = _wait_for(client, key, ("needs_paste", "failed"))
    assert job["run"]["status"] == "needs_paste"
    assert job["run"]["url"] == "https://127.0.0.1:1/job"
    [item] = client.get("/api/jobs").get_json()["items"]
    assert (item["key"], item["status"], item["company"]) == (key, "needs_paste", "New job")

    short = client.post(f"/api/job/{key}/paste", json={"text": "too short"})
    assert short.status_code == 400
    assert client.post("/api/job/run-nope/paste", json={"text": PASTED}).status_code == 404
    assert client.post(f"/api/job/{key}/paste", json={"text": PASTED}).get_json() == {"key": key}
    job = _wait_for(client, key, ("done", "failed"))
    assert job["run"]["status"] == "done", job["run"]["error"]
    posting = git(app_env.path, "show", f"{job['branch']}:job/posting.md")
    assert posting.startswith("Source: https://127.0.0.1:1/job\n\n")
    # A run that is not waiting for text refuses a paste.
    assert client.post(f"/api/job/{key}/paste", json={"text": PASTED}).status_code == 404


def test_previews_are_rebuilt_after_the_cache_is_cleared(client, app_env: ResumeFixture) -> None:
    app_env.make_tailored_branch("globex-data-intern")
    first = client.get("/api/job/globex-data-intern/original.png").data
    for path in runner.PREVIEWS_DIR.iterdir():
        path.unlink()
    assert client.get("/api/job/globex-data-intern/original.png").data == first
    assert (runner.PREVIEWS_DIR / ".nobackup").exists()
    assert client.get("/api/job/missing/original.png").status_code == 404


def test_the_preview_cache_keeps_only_the_newest_images(client, app_env: ResumeFixture, monkeypatch) -> None:
    app_env.make_tailored_branch("globex-data-intern")
    monkeypatch.setattr(runner, "MAX_PREVIEWS", 3)
    runner.PREVIEWS_DIR.mkdir(parents=True)
    for i in range(5):
        stale = runner.PREVIEWS_DIR / f"stale{i}.png"
        stale.write_bytes(b"x")
        os.utime(stale, (1000 + i, 1000 + i))
    client.get("/api/job/globex-data-intern/tailored.png")
    kept = sorted(p.name for p in runner.PREVIEWS_DIR.glob("*.png"))
    assert len(kept) == 3
    assert {"stale4.png", "stale3.png"} <= set(kept)
    assert not {"stale0.png", "stale1.png", "stale2.png"} & set(kept)
    assert (runner.PREVIEWS_DIR / ".nobackup").exists()
