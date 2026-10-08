"""Fixtures: a throwaway resume repo with a local bare origin, and the app pointed at it.

Nothing here touches the user's resume repo, GitHub, or a real Claude: the
tailoring agent and the job-naming completion are ``test_data/fake_claude.py``,
and RenderCV is ``test_data/fake_render.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from resume_tailor import review, runner, tailor
from resume_tailor.repo import ResumeRepo

TEST_DATA = Path(__file__).parent / "test_data"
FAKE_CLAUDE = TEST_DATA / "fake_claude.py"
FAKE_RENDER = TEST_DATA / "fake_render.py"
BASE_YAML = TEST_DATA / "resume" / "base.yaml"
TAILORED_YAML = TEST_DATA / "resume" / "tailored.yaml"
STARTER_SKILL = Path(__file__).parents[2] / "resume_repo_starter" / ".claude" / "skills" / "tailor-resume" / "SKILL.md"

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def render(cwd: Path) -> None:
    subprocess.run([sys.executable, str(FAKE_RENDER), "base.yaml"], cwd=cwd, check=True)


class ResumeFixture:
    """The workspace-relative resume checkout and its bare origin."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.origin = workspace / "origin.git"
        self.path = workspace / ".external_worktrees" / "resume"
        self.repo = ResumeRepo(self.path)

    def build(self) -> None:
        git(self.workspace, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.workspace / "seed"
        git(self.workspace, "clone", "-q", str(self.origin), str(seed))
        shutil.copy(BASE_YAML, seed / "base.yaml")
        (seed / ".gitignore").write_text("rendercv_output/\n.worktrees/\n")
        skill = seed / ".claude" / "skills" / "tailor-resume"
        skill.mkdir(parents=True)
        shutil.copy(STARTER_SKILL, skill / "SKILL.md")
        render(seed)
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "resume")
        git(seed, "push", "-q", "origin", "HEAD:main")
        shutil.rmtree(seed)
        git(self.workspace, "clone", "-q", str(self.origin), str(self.path))

    def main_sha(self) -> str:
        return git(self.path, "rev-parse", "refs/heads/main").strip()

    def origin_main_sha(self) -> str:
        return git(self.origin, "rev-parse", "refs/heads/main").strip()

    def make_tailored_branch(self, slug: str, tailored: Path = TAILORED_YAML) -> str:
        """A finished job branch as a run leaves it, without running one."""
        worktree = self.repo.create_worktree(slug)
        base = git(self.path, "rev-parse", "origin/main").strip()
        shutil.copy(tailored, worktree / "base.yaml")
        render(worktree)
        job = worktree / "job"
        job.mkdir()
        (job / "meta.json").write_text(
            '{"company": "Globex", "role": "Data Intern", "url": "https://jobs.example.com/1", '
            f'"tailored_on": "2026-10-08", "base_commit": "{base}"}}'
        )
        (job / "posting.md").write_text("Source: https://jobs.example.com/1\n\n# Data Intern\n\nSpark <script>x</script>\n")
        (job / "changes.md").write_text("## Experience\n\n- Brought back Globex.\n")
        git(worktree, "add", "-A")
        git(worktree, "commit", "-q", "-m", "Tailor resume for Globex Data Intern")
        return tailor.BRANCH_PREFIX + slug


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch workspace root as cwd, with git able to commit."""
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LATCHKEY_GATEWAY", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def resume(workspace: Path) -> ResumeFixture:
    fixture = ResumeFixture(workspace)
    fixture.build()
    return fixture


@pytest.fixture
def app_env(workspace: Path, resume: ResumeFixture, monkeypatch: pytest.MonkeyPatch) -> Iterator[ResumeFixture]:
    """The app pointed at the scratch repo, relative paths as in production, fakes in place."""
    stand_in = workspace / "bin" / "claude"
    stand_in.parent.mkdir()
    stand_in.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLAUDE}" "$@"\n')
    stand_in.chmod(0o755)
    data_dir = Path("data/.apps/resume-tailor")
    monkeypatch.setattr(runner, "DATA_DIR", data_dir)
    monkeypatch.setattr(runner, "RUNS_DIR", data_dir / "runs")
    monkeypatch.setattr(runner, "PREVIEWS_DIR", data_dir / "previews")
    monkeypatch.setattr(runner, "RESUME_REPO", Path(".external_worktrees/resume"))
    monkeypatch.setattr(tailor, "CLAUDE_COMMAND", str(stand_in))
    monkeypatch.setattr(review, "RENDER_COMMAND", [sys.executable, str(FAKE_RENDER), "base.yaml"])
    # The same stand-ins for worker processes, which read these at import.
    monkeypatch.setenv("RESUME_TAILOR_CLAUDE", str(stand_in))
    monkeypatch.setenv("FAKE_TAILORED", str(TAILORED_YAML))
    monkeypatch.setenv("FAKE_RENDER", str(FAKE_RENDER))
    monkeypatch.setenv("FAKE_WORKSPACE", str(Path.cwd()))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(sys.path))
    yield resume
    _stop_workers(data_dir / "runs")


def _stop_workers(runs_dir: Path) -> None:
    """Kill any worker a test left behind (each runs in its own session)."""
    for run in tailor.list_runs(runs_dir):
        pid = run.load().get("pid")
        if pid and tailor._alive(pid, run.id):
            try:
                os.killpg(pid, 9)
            except ProcessLookupError:
                pass
