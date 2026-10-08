"""Splitting a tailored resume into reviewable changes, and undoing/restoring them."""

from __future__ import annotations

import itertools
import random
import subprocess
import sys

import pytest

from resume_tailor import review
from resume_tailor.conftest import (
    BASE_YAML,
    FAKE_RENDER,
    TAILORED_YAML,
    ResumeFixture,
    git,
)

pytestmark = pytest.mark.timeout(120)


def _diff(old: str, new: str, tmp_path) -> str:
    (tmp_path / "old.yaml").write_text(old)
    (tmp_path / "new.yaml").write_text(new)
    proc = subprocess.run(
        ["git", "diff", "--no-index", "-U0", "old.yaml", "new.yaml"], cwd=tmp_path, capture_output=True, text=True
    )
    return proc.stdout


def _fixture_changes(tmp_path) -> list[review.Change]:
    base, tailored = BASE_YAML.read_text(), TAILORED_YAML.read_text()
    return review.parse_changes(_diff(base, tailored, tmp_path), tailored, base)


def test_parse_changes_names_each_edit_as_a_person_would(tmp_path) -> None:
    changes = {(c.kind, c.label) for c in _fixture_changes(tmp_path)}
    assert changes == {
        ("revived", "Globex"),
        ("reworded", "Owned reliability end-to-end via user interviews, alert tuning and runbooks"),
        # A swap (one project hidden, a hidden one brought back) is one change.
        ("revived", "Lumen: Video Enhancement"),
        ("reworded", "Coursework: Data Structures, Operating Systems, Compilers, Automata Theory"),
        ("reworded", "Python, C++, SQL, TypeScript"),
        ("reworded", "Linux, Docker, Kubernetes, gRPC, Spark, GraphQL, Git"),
        ("reworded", "Bolded words list"),
    }


def test_bold_keyword_hunks_are_one_change(tmp_path) -> None:
    keyword_changes = [c for c in _fixture_changes(tmp_path) if c.label == "Bolded words list"]
    assert len(keyword_changes) == 1
    assert sorted(keyword_changes[0].added) == ["    - PyTorch", "    - Python", "    - Spark"]
    assert sorted(keyword_changes[0].removed) == ["    - Python", "    - React", "    - TypeScript"]


def test_hiding_a_project_is_its_own_change_when_nothing_replaces_it(tmp_path) -> None:
    base = BASE_YAML.read_text()
    new = base.replace(
        '      - name: "Pathfinder (1st place, City Hackathon)"\n        date: Mar 2025\n        highlights:\n'
        "          - Built a voice-based AI agent",
        '      # - name: "Pathfinder (1st place, City Hackathon)"\n      #   date: Mar 2025\n      #   highlights:\n'
        "      #     - Built a voice-based AI agent",
    )
    assert new != base
    [change] = review.parse_changes(_diff(base, new, tmp_path), new, base)
    assert (change.kind, change.label) == ("hidden", "Pathfinder (1st place, City Hackathon)")


def test_change_ids_are_stable_and_unique(tmp_path) -> None:
    first = [c.id for c in _fixture_changes(tmp_path)]
    assert first == [c.id for c in _fixture_changes(tmp_path)]
    assert len(set(first)) == len(first)


def test_edits_to_hidden_lines_only_are_notes(tmp_path) -> None:
    base = "highlights:\n  - Live bullet\n  # - hidden one\n"
    new = "highlights:\n  - Live bullet\n  # - hidden one, reworded\n"
    [change] = review.parse_changes(_diff(base, new, tmp_path), new, base)
    assert change.kind == "notes"


def test_added_and_removed_lines(tmp_path) -> None:
    base = "highlights:\n  - one\n  - two\n"
    added = "highlights:\n  - one\n  - two\n  - three is new\n"
    removed = "highlights:\n  - one\n"
    assert [c.kind for c in review.parse_changes(_diff(base, added, tmp_path), added, base)] == ["added"]
    assert [c.kind for c in review.parse_changes(_diff(base, removed, tmp_path), removed, base)] == ["removed"]


def test_empty_diff_has_no_changes() -> None:
    assert review.parse_changes("", "a\n", "a\n") == []


def test_long_labels_are_cut(tmp_path) -> None:
    base = "highlights:\n  - short\n"
    new = "highlights:\n  - " + "word " * 60 + "\n"
    [change] = review.parse_changes(_diff(base, new, tmp_path), new, base)
    assert len(change.label) == 110 and change.label.endswith("...")


def test_revert_hunks_restores_the_original_exactly(tmp_path) -> None:
    base, tailored = BASE_YAML.read_text(), TAILORED_YAML.read_text()
    hunks = [h for c in _fixture_changes(tmp_path) for h in c.parts]
    assert review.revert_hunks(tailored, hunks, tailored) == base


def test_revert_hunks_refuses_a_change_that_was_edited_since(tmp_path) -> None:
    tailored = TAILORED_YAML.read_text()
    [reworded] = [c for c in _fixture_changes(tmp_path) if c.label.startswith("Owned reliability")]
    edited = tailored.replace("Owned reliability end-to-end", "Owned it all")
    with pytest.raises(review.ReviewError):
        review.revert_hunks(edited, reworded.parts, tailored)


# -- against a repo -------------------------------------------------------------


@pytest.fixture
def job(resume: ResumeFixture, monkeypatch) -> ResumeFixture:
    monkeypatch.setattr(review, "RENDER_COMMAND", [sys.executable, str(FAKE_RENDER), "base.yaml"])
    resume.make_tailored_branch("globex-data-intern")
    return resume


def _branch_yaml(resume: ResumeFixture) -> str:
    return git(resume.path, "show", "tailor/globex-data-intern:base.yaml")


def test_list_changes_reads_the_branch_against_its_base(job: ResumeFixture) -> None:
    changes = review.list_changes(job.repo, "refs/heads/tailor/globex-data-intern")
    assert len(changes) == 7
    assert {c["kind"] for c in changes} == {"revived", "reworded"}
    assert review.list_undone(job.repo, "refs/heads/tailor/globex-data-intern") == []


def test_undo_commits_the_reversal_and_rebuilds(job: ResumeFixture) -> None:
    ref = "tailor/globex-data-intern"
    [globex] = [c for c in review.list_changes(job.repo, ref) if c["label"] == "Globex"]
    pdf_before = git(job.path, "rev-parse", f"{ref}:Alex_Doe_resume.pdf")
    review.undo_change(job.repo, "globex-data-intern", globex["id"])
    assert "      # - company: Globex" in _branch_yaml(job)
    assert git(job.path, "rev-parse", f"{ref}:Alex_Doe_resume.pdf") != pdf_before
    message = git(job.path, "log", "-1", "--format=%B", ref)
    assert message.startswith("Undo: Globex") and f"Undo-Change: {globex['id']}" in message
    assert [u["label"] for u in review.list_undone(job.repo, ref)] == ["Globex"]
    assert globex["id"] not in {c["id"] for c in review.list_changes(job.repo, ref)}
    # The undo also reached origin.
    assert git(job.origin, "rev-parse", f"refs/heads/{ref}") == git(job.path, "rev-parse", ref)


def test_undo_then_restore_everything_in_any_order_is_byte_identical(job: ResumeFixture) -> None:
    ref = "tailor/globex-data-intern"
    tailored = _branch_yaml(job)
    ids = [c["id"] for c in review.list_changes(job.repo, ref)]
    for change_id in ids:
        review.undo_change(job.repo, "globex-data-intern", change_id)
    assert _branch_yaml(job) == BASE_YAML.read_text()
    assert review.list_changes(job.repo, ref) == []
    undone = [u["commit"] for u in review.list_undone(job.repo, ref)]
    random.Random(7).shuffle(undone)
    for commit in undone:
        review.restore_change(job.repo, "globex-data-intern", commit)
    assert _branch_yaml(job) == tailored
    assert review.list_undone(job.repo, ref) == []
    assert sorted(c["id"] for c in review.list_changes(job.repo, ref)) == sorted(ids)


@pytest.mark.parametrize("picks", list(itertools.combinations(range(7), 3))[::5])
def test_undo_a_subset_and_restore_in_reverse_is_byte_identical(job: ResumeFixture, picks) -> None:
    ref = "tailor/globex-data-intern"
    tailored = _branch_yaml(job)
    changes = review.list_changes(job.repo, ref)
    for i in picks:
        review.undo_change(job.repo, "globex-data-intern", changes[i]["id"])
    for undone in review.list_undone(job.repo, ref):  # newest first
        review.restore_change(job.repo, "globex-data-intern", undone["commit"])
    assert _branch_yaml(job) == tailored


def test_restoring_twice_is_refused(job: ResumeFixture) -> None:
    ref = "tailor/globex-data-intern"
    review.undo_change(job.repo, "globex-data-intern", review.list_changes(job.repo, ref)[0]["id"])
    [undone] = review.list_undone(job.repo, ref)
    review.restore_change(job.repo, "globex-data-intern", undone["commit"])
    with pytest.raises(review.ReviewError, match="already restored"):
        review.restore_change(job.repo, "globex-data-intern", undone["commit"])


def test_unknown_change_is_refused(job: ResumeFixture) -> None:
    with pytest.raises(review.ReviewError, match="no longer in this version"):
        review.undo_change(job.repo, "globex-data-intern", "000000000000")


def test_a_change_that_does_not_build_leaves_the_branch_alone(job: ResumeFixture, monkeypatch) -> None:
    ref = "tailor/globex-data-intern"
    before = git(job.path, "rev-parse", ref)
    monkeypatch.setattr(review, "RENDER_COMMAND", [sys.executable, "-c", "raise SystemExit(1)"])
    with pytest.raises(review.ReviewError, match="wouldn't build"):
        review.undo_change(job.repo, "globex-data-intern", review.list_changes(job.repo, ref)[0]["id"])
    assert git(job.path, "rev-parse", ref) == before
    assert git(job.repo.worktree_path("globex-data-intern"), "status", "--porcelain") == ""


def test_unsaved_edits_in_the_job_folder_block_changes(job: ResumeFixture) -> None:
    worktree = job.repo.worktree_path("globex-data-intern")
    (worktree / "base.yaml").write_text("edited by hand\n")
    with pytest.raises(review.ReviewError, match="unsaved edits"):
        review.undo_change(job.repo, "globex-data-intern", "anything")


def test_a_missing_job_folder_is_recreated(job: ResumeFixture) -> None:
    worktree = job.repo.worktree_path("globex-data-intern")
    git(job.path, "worktree", "remove", "--force", str(worktree))
    assert review.ensure_worktree(job.repo, "globex-data-intern") == worktree
    assert (worktree / "job" / "meta.json").exists()


def test_a_branch_only_on_origin_gets_a_local_folder(job: ResumeFixture) -> None:
    git(job.path, "push", "-q", "origin", "tailor/globex-data-intern")
    git(job.path, "worktree", "remove", "--force", str(job.repo.worktree_path("globex-data-intern")))
    git(job.path, "branch", "-q", "-D", "tailor/globex-data-intern")
    git(job.path, "fetch", "-q", "origin")
    worktree = review.ensure_worktree(job.repo, "globex-data-intern")
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip() == "tailor/globex-data-intern"
