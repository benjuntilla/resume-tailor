"""Read a job posting from its link.

Most job boards (Greenhouse, Lever, Ashby, Workday, company career pages) embed
the posting as schema.org ``JobPosting`` JSON-LD for search engines, so that is
tried first; the visible page text is the fallback. Sites that need a login or
render everything client-side yield too little text, and the caller asks the
user to paste the description instead.
"""

from __future__ import annotations

import http.client
import json
import urllib.request

from bs4 import BeautifulSoup
from markdownify import markdownify
from pydantic import BaseModel

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
MIN_POSTING_CHARS = 600


class PostingUnreadable(RuntimeError):
    pass


class Posting(BaseModel):
    url: str | None
    markdown: str
    raw_html: str
    title: str | None = None
    company: str | None = None


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode(resp.headers.get_content_charset() or "utf-8", "replace")
    # Every way a link can fail to open (bad address, refused, timeout, HTTP error,
    # an unknown charset) means the same thing here: ask for a paste.
    except (OSError, http.client.HTTPException, ValueError, LookupError) as exc:
        raise PostingUnreadable(f"Couldn't open that link ({exc}).") from exc


def _job_posting_ld(soup: BeautifulSoup) -> dict | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def extract(url: str | None, html: str) -> Posting:
    soup = BeautifulSoup(html, "html.parser")
    ld = _job_posting_ld(soup)
    if ld and len(str(ld.get("description", ""))) > 200:
        org = ld.get("hiringOrganization")
        company = org.get("name") if isinstance(org, dict) else None
        title = ld.get("title")
        body = markdownify(str(ld["description"]), heading_style="ATX").strip()
        md = f"# {title or 'Job posting'}\n\n" + (f"**{company}**\n\n" if company else "") + body
        return Posting(url=url, markdown=md, raw_html=html, title=title, company=company)
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    md = markdownify(str(main), heading_style="ATX").strip()
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    if len(md) < MIN_POSTING_CHARS:
        raise PostingUnreadable("That page didn't show the job description (it may need a login).")
    return Posting(url=url, markdown=md, raw_html=html, title=title)


def read_posting(url: str) -> Posting:
    return extract(url, fetch_html(url))
