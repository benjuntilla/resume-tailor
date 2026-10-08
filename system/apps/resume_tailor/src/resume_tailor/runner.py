"""Tailor your resume to a job posting, step by step.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/resume-tailor/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/resume-tailor/`` but
  honors the ``RESUME_TAILOR_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``RESUME_TAILOR_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://resume-tailor.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import hashlib
import html
import io
import os
from pathlib import Path

import markdown as markdown_lib
import pymupdf
from flask import Flask, Response, abort, jsonify, request, send_file
from werkzeug.serving import run_simple

from resume_tailor import review, tailor
from resume_tailor.repo import ResumeRepo

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/resume-tailor/`` but is overridable via the ``RESUME_TAILOR_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/resume-tailor/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("RESUME_TAILOR_DATA_DIR", "data/.apps/resume-tailor"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``RESUME_TAILOR_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("RESUME_TAILOR_PORT", "8082"))

# The resume repo (a RenderCV project with a tailor-resume skill). Relative to the
# repo root the service runs from.
RESUME_REPO = Path(os.environ.get("RESUME_TAILOR_REPO", ".external_worktrees/resume"))

RUNS_DIR = DATA_DIR / "runs"
# Rendered page images, rebuilt on demand from the PDFs in git. Only the most
# recent ones are kept; an evicted one is simply rendered again.
PREVIEWS_DIR = DATA_DIR / "previews"
MAX_PREVIEWS = 200

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path("system/apps/system_interface/imbue/system_interface/static/_static")
SHELL_STATIC_MODULE_NAMES = ('app_contract.js', 'context_menu.js')

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT = """<script type="module">
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
</script>"""

app = Flask("resume_tailor", static_folder=None)


def _repo() -> ResumeRepo:
    return ResumeRepo(RESUME_REPO.absolute())


def _render_markdown(text: str) -> str:
    # Escape first: postings come from arbitrary web pages.
    return markdown_lib.markdown(html.escape(text), extensions=["sane_lists"])


@app.route("/")
def index() -> Response:
    page = (Path(__file__).parent / "index.html").read_text()
    response = Response(page.replace("</body>", SHELL_PAGE_SCRIPT + "</body>"), mimetype="text/html")
    # Always fetch the current page, so an update shows on the next load.
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/jobs")
def api_jobs() -> Response:
    tailor.mark_interrupted(RUNS_DIR)
    repo = _repo()
    jobs = repo.list_jobs()
    runs = {}
    for run in tailor.list_runs(RUNS_DIR):
        record = run.load()
        key = record.get("slug") or run.id
        runs.setdefault(key, record)
    items = []
    for key, record in runs.items():
        if record["status"] != "done" or key not in {j["slug"] for j in jobs}:
            items.append({
                "key": key if record.get("slug") else run_key(record["id"]),
                "company": record.get("company") or "New job",
                "role": record.get("role") or (record.get("url") or ""),
                "date": record["created"][:10],
                "branch": record.get("branch"),
                "status": record["status"],
            })
    seen = {i["key"] for i in items}
    for job in jobs:
        if job["slug"] in seen:
            continue
        status = runs.get(job["slug"], {}).get("status", "done")
        items.append({"key": job["slug"], "company": job["company"], "role": job["role"] or "", "date": job["date"],
                      "branch": job["branch"], "status": status})
    items.sort(key=lambda i: i["date"], reverse=True)
    return jsonify(items=items, github=repo.github_slug())


def run_key(run_id: str) -> str:
    return "run-" + run_id


def _find_run(key: str) -> tailor.Run | None:
    for run in tailor.list_runs(RUNS_DIR):
        if run_key(run.id) == key:
            return run
        if run.load().get("slug") == key:
            return run
    return None


@app.route("/api/job/<key>")
def api_job(key: str) -> Response:
    repo = _repo()
    run = _find_run(key)
    view = run.view() if run else None
    slug = view.get("slug") if view else key
    ref = repo.ref_for(slug) if slug else None
    if not view and not ref:
        abort(404)
    out: dict = {"key": key, "slug": slug, "run": view, "has_branch": bool(ref)}
    meta = {}
    if ref:
        meta = repo._meta(ref)
        out["branch"] = ref.split("refs/heads/", 1)[-1].split("refs/remotes/origin/", 1)[-1]
        out["has_pdf"] = bool(repo.pdf_name(ref)) and (not view or view["status"] == "done")
        changes = repo.show(ref, "job/changes.md")
        out["changes_html"] = _render_markdown(changes) if changes else None
        out["diff"] = repo.resume_diff(ref)
        base = repo.base_commit(ref)
        out["resume_before"] = repo.show(base, "base.yaml") if base else None
        out["resume_after"] = repo.show(ref, "base.yaml")
        out["changes"] = review.list_changes(repo, ref)
        out["undone"] = review.list_undone(repo, ref)
        out["sha"] = repo.git("rev-parse", ref).strip()
        posting = repo.show(ref, "job/posting.md")
    else:
        posting = None
    if posting is None and run and (run.dir / "posting.md").exists():
        posting = (run.dir / "posting.md").read_text()
    out["posting_html"] = _render_markdown(posting) if posting else None
    out["company"] = (view or {}).get("company") or meta.get("company") or slug
    out["role"] = (view or {}).get("role") or meta.get("role")
    out["url"] = (view or {}).get("url") or meta.get("url")
    out["date"] = meta.get("tailored_on") or ((view or {}).get("created") or "")[:10]
    out["github"] = repo.github_slug()
    return jsonify(out)


@app.route("/api/tailor", methods=["POST"])
def api_tailor() -> Response:
    body = request.get_json(force=True)
    url = (body.get("url") or "").strip() or None
    text = (body.get("text") or "").strip() or None
    if not url and not text:
        return jsonify(error="Paste a job link first."), 400
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run = tailor.new_run(RUNS_DIR, url)
    tailor.start(_repo(), run, pasted_text=text)
    return jsonify(key=run_key(run.id))


@app.route("/api/job/<key>/paste", methods=["POST"])
def api_paste(key: str) -> Response:
    run = _find_run(key)
    text = (request.get_json(force=True).get("text") or "").strip()
    if not run or run.load()["status"] != "needs_paste":
        abort(404)
    if len(text) < 200:
        return jsonify(error="That looks too short to be a full job description."), 400
    run.update(status="running", error=None)
    tailor.start(_repo(), run, pasted_text=text)
    return jsonify(key=key)


def _review_action(slug: str, action) -> Response:
    run = _find_run(slug)
    if run and run.load()["status"] in ("running", "queued"):
        return jsonify(error="Wait for the tailoring to finish first."), 409
    try:
        action()
    except review.ReviewError as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(ok=True)


@app.route("/api/job/<slug>/changes/<change_id>/undo", methods=["POST"])
def api_undo(slug: str, change_id: str) -> Response:
    return _review_action(slug, lambda: review.undo_change(_repo(), slug, change_id))


@app.route("/api/job/<slug>/undone/<commit>/restore", methods=["POST"])
def api_restore(slug: str, commit: str) -> Response:
    return _review_action(slug, lambda: review.restore_change(_repo(), slug, commit))


@app.route("/api/refresh", methods=["POST"])
def api_refresh() -> Response:
    _repo().fetch()
    return jsonify(ok=True)


def _pdf_bytes(slug: str, which: str) -> bytes:
    repo = _repo()
    ref = repo.ref_for(slug)
    if not ref:
        abort(404)
    if which == "original":
        base = repo.base_commit(ref)
        if not base:
            abort(404)
        ref = base
    name = repo.pdf_name(ref)
    data = repo.git_bytes("show", f"{ref}:{name}") if name else None
    if data is None:
        abort(404)
    return data


@app.route("/api/job/<slug>/<which>.pdf")
def api_pdf(slug: str, which: str) -> Response:
    if which not in ("tailored", "original"):
        abort(404)
    data = _pdf_bytes(slug, which)
    filename = f"resume-{slug}.pdf" if which == "tailored" else "resume-original.pdf"
    return send_file(io.BytesIO(data), mimetype="application/pdf", download_name=filename,
                     as_attachment=request.args.get("download") == "1")


@app.route("/api/job/<slug>/<which>.png")
def api_preview(slug: str, which: str) -> Response:
    if which not in ("tailored", "original"):
        abort(404)
    data = _pdf_bytes(slug, which)
    digest = hashlib.sha256(data).hexdigest()[:20]
    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    (PREVIEWS_DIR / ".nobackup").touch()
    path = PREVIEWS_DIR / f"{digest}.png"
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pages = len(doc)
        if not path.exists():
            doc[0].get_pixmap(dpi=150).save(str(path))
            _prune_previews(keep=path)
    response = send_file(path.absolute(), mimetype="image/png")
    response.headers["X-Page-Count"] = str(pages)
    return response


def _prune_previews(keep: Path) -> None:
    """Hold the preview cache to the ``MAX_PREVIEWS`` most recently made images."""
    images = sorted(PREVIEWS_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in images[MAX_PREVIEWS:]:
        if old != keep:
            old.unlink(missing_ok=True)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    tailor.mark_interrupted(RUNS_DIR)
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
