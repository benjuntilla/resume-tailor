"""Review a tailored version change by change: list, undo, restore.

A "change" is a group of hunks of the zero-context diff of ``base.yaml`` between
the job's starting point on main and its branch: the edits a person would name
as one thing (a reworded bullet, a project brought back, the bold keyword list).
Undoing one reverse-applies just those hunks in the job's worktree, rebuilds the
PDF, and commits; the undo commit carries an ``Undo-Change:`` trailer, so git is
the record of what was undone, and a restore applies the same hunks forward.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import threading
from pathlib import Path

from pydantic import BaseModel, Field

from resume_tailor.repo import BRANCH_PREFIX, GitError, ResumeRepo

RENDER_COMMAND = ["uvx", "rendercv[full]@2.8", "render", "base.yaml"]
RESUME_SOURCE = "base.yaml"
UNDO_TRAILER = "Undo-Change"
RESTORE_TRAILER = "Restores-Undo"

ENTRY_RE = re.compile(r"^\s*(?:#\s*)?-\s+(company|name|institution|label):")
SECTION_RE = re.compile(r"^\s*(bold_keywords|render_command|design|locale)\s*:")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_job_locks: dict[str, threading.Lock] = {}
_job_locks_guard = threading.Lock()


class ReviewError(RuntimeError):
    pass


class _Hunk(BaseModel):
    lines: list[str]
    new_start: int
    new_len: int
    entry: int  # index of the entry/section line the hunk sits in, -1 if none
    entry_line: str

    @property
    def removed(self) -> list[str]:
        return [line[1:] for line in self.lines[1:] if line.startswith("-")]

    @property
    def added(self) -> list[str]:
        return [line[1:] for line in self.lines[1:] if line.startswith("+")]

    @property
    def structural(self) -> bool:
        return any(_is_comment(t) or ENTRY_RE.match(t) for t in self.removed + self.added)


class Change(BaseModel):
    id: str
    kind: str  # revived | hidden | reworded | added | removed | notes
    label: str
    removed: list[str]
    added: list[str]
    hunk: str = Field(repr=False)
    parts: list[_Hunk] = Field(default_factory=list, repr=False)


def _job_lock(slug: str) -> threading.Lock:
    with _job_locks_guard:
        return _job_locks.setdefault(slug, threading.Lock())


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _uncomment(line: str) -> str:
    return re.sub(r"^\s*#\s?", "", line).strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace('"', "").replace("'", "")).strip()


def _readable(line: str) -> str:
    """A YAML line as a person would read it."""
    text = _uncomment(line) if _is_comment(line) else line.strip()
    text = re.sub(r"^-\s+", "", text)
    if not text.startswith(("http", "[")):
        text = re.sub(r"^[A-Za-z_]+:\s*", "", text)
    return text.strip().strip("'\"").replace("**", "")


class _Context(BaseModel):
    """What the original resume had live and hidden, to recognise revived lines."""

    base_live: set[str]
    base_hidden: set[str]
    new_live: set[str]

    @classmethod
    def from_texts(cls, base_text: str, new_text: str) -> _Context:
        base_lines = base_text.splitlines()
        new_lines = new_text.splitlines()
        return cls(
            base_live={_norm(line) for line in base_lines if line.strip() and not _is_comment(line)},
            base_hidden={_norm(_uncomment(line)) for line in base_lines if _is_comment(line)},
            new_live={_norm(line) for line in new_lines if line.strip() and not _is_comment(line)},
        )


def _revived(added: list[str], ctx: _Context) -> list[str]:
    """Added live lines that were hidden (commented out) in the original."""
    return [a for a in added if a.strip() and not _is_comment(a)
            and _norm(a) in ctx.base_hidden and _norm(a) not in ctx.base_live and len(_norm(a)) > 3]


def _hidden(removed: list[str], added: list[str]) -> list[str]:
    """Removed live lines that now appear commented out."""
    commented = {_norm(_uncomment(a)) for a in added if _is_comment(a)}
    return [r for r in removed if r.strip() and not _is_comment(r) and _norm(r) in commented]


def _kind(removed: list[str], added: list[str], ctx: _Context) -> str:
    if all(_is_comment(line) or not line.strip() for line in removed + added):
        return "notes"  # only hidden lines moved or edited; the PDF is unaffected
    if _revived(added, ctx):
        return "revived"
    live_removed = [r for r in removed if r.strip() and not _is_comment(r)]
    if live_removed and len(_hidden(removed, added)) * 2 >= len(live_removed):
        return "hidden"
    if added and removed:
        return "reworded"
    return "added" if added else "removed"


def _label(removed: list[str], added: list[str], kind: str) -> str:
    source = removed if kind in ("hidden", "removed") else (added or removed)
    for line in source:
        text = _readable(line)
        if len(text) > 3:
            return text if len(text) <= 110 else text[:107] + "..."
    return "Formatting change"


def _entry_for(new_lines: list[str], line_no: int) -> tuple[int, str]:
    for i in range(min(line_no, len(new_lines)) - 1, -1, -1):
        if SECTION_RE.match(new_lines[i]) or ENTRY_RE.match(new_lines[i]):
            return i, new_lines[i]
    return -1, ""


def parse_changes(diff: str, new_text: str = "", base_text: str = "") -> list[Change]:
    """Group the hunks of a zero-context diff into the changes a person would name.

    Hunks merge when they sit in the same entry (an experience, project, school or
    skills row) and one of them restructures it (hides, revives, or edits its
    header) with at most two lines between them, or when both are in the bold
    keyword list.
    """
    new_lines = new_text.splitlines()
    hunks: list[_Hunk] = []
    current: list[str] | None = None
    for line in diff.splitlines() + ["@@ end"]:
        if line.startswith("@@"):
            if current:
                m = HUNK_RE.match(current[0])
                new_start = int(m.group(3)) if m else 0
                new_len = int(m.group(4)) if m and m.group(4) is not None else 1
                entry, entry_line = _entry_for(new_lines, new_start if new_len else new_start + 1)
                hunks.append(_Hunk(lines=current, new_start=new_start, new_len=new_len, entry=entry, entry_line=entry_line))
            current = [line]
        elif current is not None and line[:1] in ("+", "-", "\\") and not line.startswith(("+++", "---")):
            current.append(line)
    groups: list[list[_Hunk]] = []
    for hunk in hunks:
        prev = groups[-1][-1] if groups else None
        if prev and hunk.entry >= 0 and prev.entry == hunk.entry:
            gap = hunk.new_start - (prev.new_start + prev.new_len)
            in_keywords = "bold_keywords" in hunk.entry_line
            if in_keywords or ((prev.structural or hunk.structural) and gap <= 2):
                groups[-1].append(hunk)
                continue
        groups.append([hunk])
    ctx = _Context.from_texts(base_text, new_text)
    return [_make_change(group, ctx) for group in groups]


def _make_change(group: list[_Hunk], ctx: _Context) -> Change:
    removed = [r for h in group for r in h.removed]
    added = [a for h in group for a in h.added]
    signature = "\n".join("-" + r for r in removed) + "\n" + "\n".join("+" + a for a in added)
    kind = _kind(removed, added, ctx)
    entry_line = group[0].entry_line
    if "bold_keywords" in entry_line:
        label = "Bolded words list"
    elif kind == "revived":
        revived = _revived(added, ctx)
        headers = [line for line in revived if ENTRY_RE.match(line)]
        label = _readable((headers or revived)[0])
    elif kind == "hidden":
        hidden = _hidden(removed, added)
        headers = [line for line in hidden if ENTRY_RE.match(line)]
        label = _readable((headers or hidden)[0])
    else:
        label = _label(removed, added, kind)
    if len(label) > 110:
        label = label[:107] + "..."
    return Change(
        id=hashlib.sha1(signature.encode()).hexdigest()[:12], kind=kind, label=label,
        removed=removed, added=added, hunk="".join("\n".join(h.lines) + "\n" for h in group), parts=group,
    )


def _zero_context_diff(repo: ResumeRepo, old: str, new: str) -> str:
    return repo.git("diff", "-U0", old, new, "--", RESUME_SOURCE, check=False)


def _changes(repo: ResumeRepo, ref: str) -> list[Change]:
    base = repo.base_commit(ref)
    if not base:
        return []
    return parse_changes(
        _zero_context_diff(repo, base, ref), repo.show(ref, RESUME_SOURCE) or "", repo.show(base, RESUME_SOURCE) or ""
    )


def list_changes(repo: ResumeRepo, ref: str) -> list[dict]:
    return [
        {"id": c.id, "kind": c.kind, "label": c.label, "removed": c.removed, "added": c.added}
        for c in _changes(repo, ref)
    ]


def list_undone(repo: ResumeRepo, ref: str) -> list[dict]:
    """Undo commits on the branch that no later commit restored, newest first."""
    base = repo.base_commit(ref)
    if not base:
        return []
    log = repo.git("log", "--format=%H%x1f%s%x1f%b%x1e", f"{base}..{ref}", check=False)
    undone: dict[str, dict] = {}
    restored: set[str] = set()
    for entry in log.split("\x1e"):
        parts = entry.strip().split("\x1f")
        if len(parts) < 3:
            continue
        sha, subject, body = parts
        if f"{RESTORE_TRAILER}:" in body:
            restored.add(body.split(f"{RESTORE_TRAILER}:", 1)[1].split()[0])
        elif f"{UNDO_TRAILER}:" in body:
            undone[sha] = {"commit": sha, "label": subject.removeprefix("Undo: ")}
    return [u for sha, u in undone.items() if sha not in restored]


# -- mutations ----------------------------------------------------------------


def ensure_worktree(repo: ResumeRepo, slug: str) -> Path:
    """The job's worktree, recreating it (or the local branch) when missing."""
    worktree = repo.worktree_path(slug)
    if (worktree / ".git").exists():
        return worktree
    branch = BRANCH_PREFIX + slug
    repo.git("worktree", "prune")
    if repo._ref_ok(f"refs/heads/{branch}"):
        repo.git("worktree", "add", "-q", str(worktree), branch)
    else:
        repo.git("worktree", "add", "-q", "--track", "-b", branch, str(worktree), f"origin/{branch}")
    return worktree


def revert_hunks(current: str, hunks: list[_Hunk], source: str) -> str:
    """Replace each hunk's new-side lines in ``current`` with its old-side lines.

    ``source`` is the file the hunks' new side was computed from. When
    ``current`` is that file the positions are exact; otherwise each hunk is
    found near its expected line by its own lines, or, for a pure insertion
    point, by the source line just above it.
    """
    lines = current.splitlines()
    source_lines = source.splitlines()
    for hunk in sorted(hunks, key=lambda h: h.new_start, reverse=True):
        start = hunk.new_start - 1 if hunk.new_len else hunk.new_start
        new_side, old_side = hunk.added, hunk.removed
        at = _locate(lines, start, new_side, source_lines[start - 1] if start > 0 and start - 1 < len(source_lines) else None)
        if at is None:
            raise ReviewError("That change overlaps another edit made since, so it can't be changed on its own.")
        lines[at:at + len(new_side)] = old_side
    return "\n".join(lines) + "\n"


def _locate(lines: list[str], expected: int, block: list[str], anchor: str | None) -> int | None:
    for delta in sorted(range(-60, 61), key=abs):
        at = expected + delta
        if at < 0 or at > len(lines):
            continue
        if block:
            if lines[at:at + len(block)] == block:
                return at
        elif anchor is None or (at > 0 and lines[at - 1] == anchor):
            return at
    return None


def _apply(worktree: Path, hunks: list[_Hunk], source: str) -> None:
    path = worktree / RESUME_SOURCE
    path.write_text(revert_hunks(path.read_text(), hunks, source))


def _render_and_commit(repo: ResumeRepo, worktree: Path, message: str) -> None:
    proc = subprocess.run(RENDER_COMMAND, cwd=worktree, capture_output=True, text=True, check=False, timeout=600)
    if proc.returncode != 0:
        repo.git("checkout", "--", ".", cwd=worktree)
        raise ReviewError("The resume wouldn't build that way, so nothing was changed.")
    repo.git("add", "-A", cwd=worktree)
    repo.git("commit", "-q", "-m", message, cwd=worktree)


def _prepare(repo: ResumeRepo, slug: str) -> Path:
    worktree = ensure_worktree(repo, slug)
    if repo.git("status", "--porcelain", cwd=worktree).strip():
        raise ReviewError("This version has unsaved edits in its folder; finish or discard those first.")
    return worktree


def undo_change(repo: ResumeRepo, slug: str, change_id: str) -> None:
    with _job_lock(slug):
        worktree = _prepare(repo, slug)
        branch = BRANCH_PREFIX + slug
        change = next((c for c in _changes(repo, branch) if c.id == change_id), None)
        if not change:
            raise ReviewError("That change is no longer in this version. Reload to see the current list.")
        _apply(worktree, change.parts, repo.show(branch, RESUME_SOURCE) or "")
        _render_and_commit(repo, worktree, f"Undo: {change.label}\n\n{UNDO_TRAILER}: {change.id}")
        _push_quietly(repo, branch)


def restore_change(repo: ResumeRepo, slug: str, undo_commit: str) -> None:
    with _job_lock(slug):
        worktree = _prepare(repo, slug)
        branch = BRANCH_PREFIX + slug
        if undo_commit not in {u["commit"] for u in list_undone(repo, branch)}:
            raise ReviewError("That change was already restored. Reload to see the current list.")
        label = repo.git("log", "-1", "--format=%s", undo_commit).strip().removeprefix("Undo: ")
        undo_diff = _zero_context_diff(repo, f"{undo_commit}^", undo_commit)
        after_undo = repo.show(undo_commit, RESUME_SOURCE) or ""
        hunks = [h for c in parse_changes(undo_diff, after_undo) for h in c.parts]
        _apply(worktree, hunks, after_undo)
        _render_and_commit(repo, worktree, f"Restore: {label}\n\n{RESTORE_TRAILER}: {undo_commit}")
        _push_quietly(repo, branch)


def _push_quietly(repo: ResumeRepo, branch: str) -> None:
    try:
        repo.push(branch)
    except GitError:
        pass  # saved locally; the next push carries it
