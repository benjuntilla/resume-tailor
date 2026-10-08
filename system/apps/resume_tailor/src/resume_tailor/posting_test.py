"""Reading a job posting from a page: JSON-LD first, page text as the fallback."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from resume_tailor import posting

POSTINGS = Path(__file__).parent / "test_data" / "postings"


def _html(name: str) -> str:
    return (POSTINGS / name).read_text()


def test_json_ld_posting_is_read_with_company_and_title() -> None:
    result = posting.extract("https://jobs.example.com/1", _html("jsonld.html"))
    assert result.title == "ML Infrastructure Intern"
    assert result.company == "Acme Robotics"
    assert result.raw_html == _html("jsonld.html")
    assert result.markdown.startswith("# ML Infrastructure Intern\n\n**Acme Robotics**\n\n")
    assert "### What you'll do" in result.markdown
    assert "* Build training infrastructure in Python and Spark" in result.markdown
    assert "Loading..." not in result.markdown


def test_json_ld_inside_a_graph_with_a_plain_company_name() -> None:
    result = posting.extract(None, _html("jsonld_graph.html"))
    assert result.title == "ML Infrastructure Intern"
    assert result.company is None  # hiringOrganization was a bare string
    assert result.markdown.startswith("# ML Infrastructure Intern\n\n")
    assert "Own reliability of model deployments on Kubernetes" in result.markdown


def test_page_text_is_the_fallback_without_page_chrome() -> None:
    result = posting.extract("https://globex.example/jobs/7", _html("plain.html"))
    assert result.title == "Data Engineer Intern | Globex"
    assert result.company is None
    assert result.markdown.startswith("# Data Engineer Intern")
    assert "Responsibility number 11" in result.markdown
    for chrome in ("Home Jobs About", "Globex Careers", "Copyright", "track()"):
        assert chrome not in result.markdown


def test_a_page_without_the_description_asks_for_a_paste() -> None:
    with pytest.raises(posting.PostingUnreadable, match="didn't show the job description"):
        posting.extract("https://jobs.example.com/2", _html("login_wall.html"))


class _Pages(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 -- the handler's API
        if self.path == "/job":
            body = _html("jsonld.html").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_read_posting_fetches_the_link(server: str) -> None:
    result = posting.read_posting(server + "/job")
    assert result.url == server + "/job"
    assert result.company == "Acme Robotics"


@pytest.mark.parametrize("path", ["/missing"])
def test_an_http_error_asks_for_a_paste(server: str, path: str) -> None:
    with pytest.raises(posting.PostingUnreadable, match="Couldn't open that link"):
        posting.read_posting(server + path)


@pytest.mark.parametrize("url", ["http://127.0.0.1:1/job", "https://", "https://bad host/"])
def test_an_unreachable_or_malformed_link_asks_for_a_paste(url: str) -> None:
    with pytest.raises(posting.PostingUnreadable):
        posting.read_posting(url)
