---
title: "Resume Tailor"
description: "Paste a job link and watch an AI agent tailor your RenderCV resume to it step by step, then keep or undo each change. One git branch per job."
thumbnail: "template.svg"
version: v1
format: v2
---

# Resume Tailor

This file is the manifest for the **Resume Tailor** template (slug:
`resume-tailor`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a workspace that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

Paste a job link and watch an AI agent tailor your RenderCV resume to it step by step, then keep or undo each change. One git branch per job.

Resume Tailor is a desktop app for anyone who keeps their resume as a
[RenderCV](https://rendercv.com) YAML file in a git repo and tailors it for each
application. The user pastes a job posting link (or the description itself) and
clicks "Tailor resume". The app reads the posting, gives the job its own copy of
the resume, and runs an AI agent that follows the repo's own `tailor-resume`
instructions: reorder skills, reword experience bullets, swap projects, update
coursework and bolded keywords, then render the PDF and check it fits on one
page. Each of those steps shows live in the window as it happens. When it
finishes, the user sees the tailored PDF beside the original and the posting,
plus a "Review edits" tab listing every change with an Undo button (and Restore
for anything undone). Items the agent brought back from commented-out (hidden)
entries are flagged so the user checks they are still accurate. Every job is
kept as its own `tailor/<company>-<role>` git branch with the posting and a
change log, so past jobs stay one click away in the sidebar and the general
resume on `main` is never touched.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original agent onto a clean default-workspace-template base):

- `system/apps/resume_tailor`
- `system/supervisord.conf.d/resume-tailor.conf`

- `system/apps/resume_tailor/` -- the app itself, a Python package (`resume_tailor`)
  with a Flask backend and a single-page frontend (`src/resume_tailor/index.html`):
  - `runner.py` is the web server (port 8082, override with `RESUME_TAILOR_PORT`)
    and the job list/API; `worker.py` runs each tailoring as its own detached
    process so an app restart does not kill a run, one at a time with later ones
    queued.
  - `tailor.py` drives one run: `posting.py` reads the posting (JSON-LD
    `JobPosting` first, page text as a fallback; a login wall asks for a paste),
    a short Claude Haiku completion names the job, `repo.py` creates branch
    `tailor/<slug>` and worktree `.worktrees/<slug>` off a freshly fetched
    `origin/main`, then a headless `claude -p --output-format stream-json` agent
    (Claude Opus) follows `.claude/skills/tailor-resume/SKILL.md` inside that
    worktree; its stream is mapped onto the step list shown in the window.
    The app then commits and pushes the branch.
  - `review.py` splits the branch's edit to `base.yaml` into individual changes;
    undo and restore are new commits on the job branch, followed by a re-render
    (`uvx 'rendercv[full]@2.8' render base.yaml`) and a push.
  - `claude_p.py` is the workspace's copyable helper for calling `claude -p`
    from a service.
  - `resume_repo_starter/.claude/skills/tailor-resume/SKILL.md` is a starter
    copy of the instructions the agent follows, to drop into a resume repo
    that does not have them yet.
  - `app.toml` registers the window (name, icon, preview settings); tests sit
    beside each module and use stand-ins for Claude and RenderCV.
- `system/supervisord.conf.d/resume-tailor.conf` -- the supervisord program
  `resume-tailor`. It registers the app's URL (`http://localhost:8082`) with
  `forward_port.py` from the app's `app.toml`, then execs the `resume-tailor`
  entry point. It stays running even with no window open
  (`stop_when_no_windows = false`) because a tailoring run can outlive the window.

At runtime the app works on the resume repo at `RESUME_TAILOR_REPO`
(default `.external_worktrees/resume`, relative to the workspace root). Fetch
and push go through the latchkey gateway (`LATCHKEY_GATEWAY`) when the repo's
`origin` is on GitHub, so no GitHub token is stored in the workspace. Run
records (step progress, the agent's full transcript, the fetched posting page)
are kept under `data/.apps/resume-tailor/runs/`; the job branch itself carries
`job/meta.json`, `job/posting.md`, and `job/changes.md`.

## Recipe

This template is version `v1`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[`template.toml`](template.toml) -- its `[recipe]` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting agent must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable `requires_` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.


Activation:

- requires_permission: github-git / github-git-read (fetch the resume repo's `main` and existing `tailor/*` branches before each run and on "Refresh"; user-approved, the adopting agent initiates this via a latchkey permission request during setup)
- requires_permission: github-git / github-git-write (push each job's `tailor/<company>-<role>` branch after a run and after every undo/restore; user-approved, the adopting agent initiates this via a latchkey permission request during setup)
- requires_llm: calls Claude KEYLESS through `claude -p` (the subscription credit pool): a Claude Haiku completion names each job and a Claude Opus agent run (`claude -p --output-format stream-json`) does the tailoring, with the model names fixed in `system/apps/resume_tailor/src/resume_tailor/tailor.py`. An adopter on the keyed path (`ANTHROPIC_API_KEY` set) needs no code change, since `claude -p` uses the key itself, but should know a tailoring cost about $1 on the publisher's runs; to route the naming completion through litellm instead, switch it per the use-ai-integration skill.

Adaptation:

- **The user's own resume repo is not included.** The app expects a git repo
  with a RenderCV `base.yaml` on `main`, an `origin` on GitHub, `.worktrees/`
  gitignored, and the tailoring instructions at
  `.claude/skills/tailor-resume/SKILL.md`, cloned at `RESUME_TAILOR_REPO`
  (default `.external_worktrees/resume`). Ask the user for their resume repo
  (or help them create one from their existing resume), clone it there, and
  copy `system/apps/resume_tailor/resume_repo_starter/.claude/` into it if it
  has no tailor-resume skill yet.
- **The tailoring instructions assume a specific resume shape.** The starter
  skill and the app's step list name sections a new-grad engineering resume has
  (skills in two rows, experience, projects, coursework, `bold_keywords`, one
  page). If the user's resume is organised differently, edit their repo's
  `SKILL.md` to match, and update `STEPS` / `AGENT_STEP_KEYS` in `tailor.py` so
  the live step list matches what the agent actually does.
- **RenderCV is pinned to 2.8.** The render command (`uvx 'rendercv[full]@2.8'`)
  is fixed in `review.py`, `tailor.py`, and the starter skill. If the user's
  `base.yaml` targets another RenderCV version (or their repo renders it in
  CI with another version), change all three to match.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting agent
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that agent's environment rather than frozen to
whatever this publisher happened to have.

Nothing extra -- runs on the stock workspace environment.

The app shells out to `git`, `claude`, and `uvx`, all of which the stock
workspace already has. Two things are fetched at run time rather than installed:
`uvx` downloads `rendercv[full]==2.8` into its cache on the first render, and the
"Review edits" tab loads the `@pierre/diffs@1.5.2` diff viewer from esm.sh in
the browser. Both need internet access from the workspace and the browser.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new agent. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `latchkey` skill; the request
   opens the approval/login flow in the Imbue Studio app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-10-08) -- first release: the Resume Tailor app (paste a job link, watch the agent tailor a RenderCV resume step by step, review and undo each change, one branch per job) plus a starter tailor-resume skill for the resume repo.

## Adaptation history

Each agent that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
