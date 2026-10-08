"""Git operations on the resume repo: one branch + worktree per tailored job.

The repo follows the layout its own tailor-resume skill describes: ``main`` holds
the general resume, each job lives on ``tailor/<slug>`` checked out at
``.worktrees/<slug>``, and the branch carries a ``job/`` record
(``meta.json``, ``posting.md``, ``changes.md``).

Network operations (fetch, push) go through the latchkey gateway when this
workspace has one (``LATCHKEY_GATEWAY``), so no GitHub token lives here;
otherwise they use the repo's own ``origin`` and whatever credentials git has.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

BRANCH_PREFIX = "tailor/"
WORKTREES_DIR = ".worktrees"


class GitError(RuntimeError):
    pass


class ResumeRepo:
    def __init__(self, path: Path) -> None:
        self.path = path

    # -- plumbing ---------------------------------------------------------

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=cwd or self.path, capture_output=True, text=True, check=False
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:400]}")
        return proc.stdout

    def git_bytes(self, *args: str) -> bytes | None:
        proc = subprocess.run(["git", *args], cwd=self.path, capture_output=True, check=False)
        return proc.stdout if proc.returncode == 0 else None

    def origin_url(self) -> str:
        return self.git("remote", "get-url", "origin").strip()

    def github_slug(self) -> str | None:
        """``owner/repo`` when origin is on GitHub, else None."""
        m = re.match(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?$", self.origin_url())
        return m.group(1) if m else None

    def _remote_args(self) -> tuple[list[str], str]:
        """The ``-c`` options and URL to reach origin with (gateway when available)."""
        gateway = os.environ.get("LATCHKEY_GATEWAY")
        url = self.origin_url()
        if gateway and url.startswith("https://github.com/"):
            opts = ["-c", f"http.extraHeader=X-Latchkey-Gateway-Password: {os.environ.get('LATCHKEY_GATEWAY_PASSWORD', '')}"]
            override = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE")
            if override:
                opts += ["-c", f"http.extraHeader=X-Latchkey-Gateway-Permissions-Override: {override}"]
            return opts, f"{gateway.rstrip('/')}/gateway/{url}"
        return [], url

    def fetch(self) -> None:
        opts, url = self._remote_args()
        self.git(*opts, "fetch", "-q", "--prune", url, "+refs/heads/*:refs/remotes/origin/*")

    def push(self, branch: str) -> None:
        opts, url = self._remote_args()
        self.git(*opts, "push", "-q", url, f"{branch}:refs/heads/{branch}")
        self.git("update-ref", f"refs/remotes/origin/{branch}", branch)
        self.git("branch", "-q", f"--set-upstream-to=origin/{branch}", branch, check=False)

    # -- jobs -------------------------------------------------------------

    def branch_exists(self, branch: str) -> bool:
        return self._ref_ok(f"refs/heads/{branch}") or self._ref_ok(f"refs/remotes/origin/{branch}")

    def free_slug(self, slug: str) -> str:
        candidate, n = slug, 2
        while self.branch_exists(BRANCH_PREFIX + candidate) or (self.path / WORKTREES_DIR / candidate).exists():
            candidate, n = f"{slug}-{n}", n + 1
        return candidate

    def create_worktree(self, slug: str) -> Path:
        """Branch ``tailor/<slug>`` off the freshly fetched ``origin/main``."""
        worktree = self.path / WORKTREES_DIR / slug
        self.git("worktree", "add", "-q", "-b", BRANCH_PREFIX + slug, str(worktree), "origin/main")
        return worktree

    def worktree_path(self, slug: str) -> Path:
        return self.path / WORKTREES_DIR / slug

    def ref_for(self, slug: str) -> str | None:
        """The best ref for a job: the local branch, else the remote one."""
        for ref in (f"refs/heads/{BRANCH_PREFIX}{slug}", f"refs/remotes/origin/{BRANCH_PREFIX}{slug}"):
            if self._ref_ok(ref):
                return ref
        return None

    def _ref_ok(self, ref: str) -> bool:
        return subprocess.run(
            ["git", "show-ref", "--verify", "-q", ref], cwd=self.path, check=False
        ).returncode == 0

    def show(self, ref: str, path: str) -> str | None:
        data = self.git_bytes("show", f"{ref}:{path}")
        return data.decode("utf-8", "replace") if data is not None else None

    def pdf_name(self, ref: str) -> str | None:
        names = self.git("ls-tree", "--name-only", ref, check=False).split()
        pdfs = [n for n in names if n.lower().endswith(".pdf")]
        return pdfs[0] if pdfs else None

    def list_jobs(self) -> list[dict]:
        """Every ``tailor/*`` branch (local or on origin) with its job record."""
        out = self.git(
            "for-each-ref",
            "--format=%(refname)\t%(committerdate:short)\t%(objectname)",
            f"refs/heads/{BRANCH_PREFIX}",
            f"refs/remotes/origin/{BRANCH_PREFIX}",
        )
        jobs: dict[str, dict] = {}
        for line in out.splitlines():
            ref, date, sha = line.split("\t")
            branch = ref.split("refs/heads/", 1)[-1].split("refs/remotes/origin/", 1)[-1]
            slug = branch[len(BRANCH_PREFIX):]
            is_local = ref.startswith("refs/heads/")
            if slug in jobs and not is_local:
                jobs[slug]["pushed"] = True
                continue
            meta = self._meta(ref)
            jobs.setdefault(slug, {}).update(
                slug=slug,
                branch=branch,
                ref=ref,
                sha=sha,
                company=meta.get("company") or slug,
                role=meta.get("role"),
                url=meta.get("url"),
                date=meta.get("tailored_on") or date,
                pushed=jobs.get(slug, {}).get("pushed", not is_local),
            )
        return sorted(jobs.values(), key=lambda j: j["date"], reverse=True)

    def _meta(self, ref: str) -> dict:
        raw = self.show(ref, "job/meta.json")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def base_commit(self, ref: str) -> str | None:
        meta = self._meta(ref)
        base = meta.get("base_commit")
        if base and self.git_bytes("cat-file", "-e", f"{base}^{{commit}}") is not None:
            return base
        merge_base = self.git("merge-base", "origin/main", ref, check=False).strip()
        return merge_base or None

    def resume_diff(self, ref: str) -> str:
        base = self.base_commit(ref)
        if not base:
            return ""
        return self.git("diff", base, ref, "--", "base.yaml", check=False)
