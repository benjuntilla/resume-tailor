# resume-tailor

Paste a job posting link; watch an AI agent tailor your resume to it step by step,
then review every change and undo the ones you don't want.

## How it works

- Your resume lives in its own git repo as a [RenderCV](https://rendercv.com) file
  (`base.yaml`), with a `tailor-resume` skill at `.claude/skills/tailor-resume/SKILL.md`
  describing how to tailor it. `resume_repo_starter/` holds a starter copy of that skill.
- The app clones nothing itself: point `RESUME_TAILOR_REPO` (default
  `.external_worktrees/resume`, relative to the workspace root) at a checkout of that repo.
- Each job gets its own branch `tailor/<company>-<role>` and worktree
  `.worktrees/<slug>`, created from `main`. A headless Claude Code agent runs the skill
  inside that worktree and reports progress; the app commits and pushes the branch.
  `main` is never changed.
- Each branch carries a `job/` record: `meta.json`, the posting as read (`posting.md`),
  and a change log (`changes.md`).
- The review tab splits the edit into changes; undo/restore are commits on the branch.
- Each run is its own process (`resume_tailor.worker`), so app restarts don't stop it;
  one runs at a time and later ones queue.

Run data (step progress, the agent's full transcript, the fetched posting page) is kept
under `data/.apps/resume-tailor/runs/`.

## Tests

`uv run pytest` from this folder. The tests build a scratch resume repo whose `origin` is a
local bare repo, and stand in for Claude (`test_data/fake_claude.py`, via
`RESUME_TAILOR_CLAUDE`, which also names the job) and for RenderCV
(`test_data/fake_render.py`), so they never touch your resume, GitHub, or a model.
