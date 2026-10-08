"""The resume repo's remote and job listing."""

from __future__ import annotations

import pytest

from resume_tailor.conftest import ResumeFixture, git
from resume_tailor.repo import GitError, ResumeRepo

pytestmark = pytest.mark.timeout(60)


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/alex/resume.git", "alex/resume"),
        ("https://github.com/alex/resume", "alex/resume"),
        ("git@github.com:alex/resume.git", "alex/resume"),
        ("/srv/git/resume.git", None),
        ("https://gitlab.com/alex/resume.git", None),
    ],
)
def test_github_slug(resume: ResumeFixture, url: str, slug: str | None) -> None:
    git(resume.path, "remote", "set-url", "origin", url)
    assert resume.repo.github_slug() == slug


def test_github_remotes_go_through_the_gateway(resume: ResumeFixture, monkeypatch) -> None:
    git(resume.path, "remote", "set-url", "origin", "https://github.com/alex/resume.git")
    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://gateway.local:9/")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "pw")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "repo")
    opts, url = resume.repo._remote_args()
    assert url == "http://gateway.local:9/gateway/https://github.com/alex/resume.git"
    assert opts == [
        "-c", "http.extraHeader=X-Latchkey-Gateway-Password: pw",
        "-c", "http.extraHeader=X-Latchkey-Gateway-Permissions-Override: repo",
    ]
    monkeypatch.delenv("LATCHKEY_GATEWAY")
    assert resume.repo._remote_args() == ([], "https://github.com/alex/resume.git")


def test_list_jobs_merges_local_and_pushed_branches(resume: ResumeFixture) -> None:
    resume.make_tailored_branch("globex-data-intern")
    resume.make_tailored_branch("initech-swe")
    resume.repo.push("tailor/globex-data-intern")
    jobs = {j["slug"]: j for j in resume.repo.list_jobs()}
    assert set(jobs) == {"globex-data-intern", "initech-swe"}
    assert jobs["globex-data-intern"]["pushed"] is True
    assert jobs["initech-swe"]["pushed"] is False
    assert jobs["globex-data-intern"]["ref"] == "refs/heads/tailor/globex-data-intern"
    assert (jobs["initech-swe"]["company"], jobs["initech-swe"]["date"]) == ("Globex", "2026-10-08")


def test_free_slug_skips_taken_names(resume: ResumeFixture) -> None:
    assert resume.repo.free_slug("globex-data-intern") == "globex-data-intern"
    resume.make_tailored_branch("globex-data-intern")
    assert resume.repo.free_slug("globex-data-intern") == "globex-data-intern-2"
    (resume.path / ".worktrees" / "globex-data-intern-2").mkdir()
    assert resume.repo.free_slug("globex-data-intern") == "globex-data-intern-3"


def test_base_commit_falls_back_to_the_merge_base(resume: ResumeFixture) -> None:
    resume.make_tailored_branch("globex-data-intern")
    worktree = resume.repo.worktree_path("globex-data-intern")
    (worktree / "job" / "meta.json").write_text("not json")
    git(worktree, "commit", "-qam", "break meta")
    assert resume.repo.base_commit("tailor/globex-data-intern") == resume.origin_main_sha()
    assert resume.repo.show("tailor/globex-data-intern", "missing.txt") is None


def test_git_errors_name_the_command(tmp_path) -> None:
    with pytest.raises(GitError, match="git rev-parse HEAD failed"):
        ResumeRepo(tmp_path).git("rev-parse", "HEAD")
