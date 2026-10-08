"""The page in a real browser: open a tailored job, review its edits, undo one."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect
from werkzeug.serving import make_server

from resume_tailor import runner
from resume_tailor.conftest import ResumeFixture

pytestmark = [pytest.mark.browser, pytest.mark.timeout(120, func_only=False)]


@pytest.fixture
def app_url(app_env: ResumeFixture) -> Iterator[str]:
    app_env.make_tailored_branch("globex-data-intern")
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_review_a_job_and_undo_a_change(page: Page, app_url: str) -> None:
    # Keep the test offline: the diff viewer's CDN import fails and the plain diff shows.
    page.route("https://esm.sh/**", lambda route: route.abort())
    page.goto(app_url + "/")
    item = page.locator("#job-globex-data-intern")
    expect(item).to_contain_text("Globex")
    expect(item).to_contain_text("tailor/globex-data-intern")
    expect(page.locator(".jobhead h2")).to_have_text("Globex")
    expect(page.locator(".jobhead")).to_contain_text("Open original posting")
    expect(page.locator(".revived-banner")).to_contain_text("2 items were brought back from your hidden list")
    expect(page.locator("#page-tailored .badge")).to_have_text("Fits on one page")

    page.locator(".tab[data-tab=posting]").click()
    expect(page.locator("#tabBody .md h1")).to_have_text("Data Intern")

    page.locator(".tab[data-tab=edits]").click()
    expect(page.locator(".rsum")).to_have_text(
        "7 changes from your main resume. Undo any you don't want: the resume rebuilds and the change is saved."
    )
    globex = page.locator(".ccard", has=page.locator(".clabel", has_text="Globex"))
    expect(globex.locator(".kind")).to_have_text("Brought back from hidden")
    expect(globex.locator(".cdiff .a").first).to_contain_text("company: Globex")
    globex.locator(".undo").click()
    expect(page.locator(".undone .urow")).to_have_count(1)
    expect(page.locator(".undone .urow span")).to_have_text("Globex")
    expect(page.locator(".rsum")).to_contain_text("6 changes")

    page.locator(".restore").click()
    expect(page.locator(".undone")).to_have_count(0)
    expect(page.locator(".rsum")).to_contain_text("7 changes")


def test_an_empty_workspace_shows_the_welcome(page: Page, app_env: ResumeFixture) -> None:
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page.goto(f"http://127.0.0.1:{server.server_port}/")
        expect(page.locator("#jobList")).to_contain_text("Nothing yet.")
        expect(page.locator(".welcome h2")).to_have_text("Tailor your resume to a job")
        page.locator("#go").click()
        expect(page.locator("#formErr")).to_have_text("Paste a job link first.")
    finally:
        server.shutdown()
