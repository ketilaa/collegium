"""Reading RSS and Atom feeds that the owner has approved for a domain.

A feed item is a lead like a search result. Feeds are fetched as they are,
with no query, so what is sent out is only the feed's own URL. XML from
outside is parsed with defusedxml, which refuses entity-expansion attacks.
"""

import html
import re

import httpx
from defusedxml import ElementTree

from collegium.acquisition import SearchResult

USER_AGENT = "Collegium/0.1 (research; read-only)"
_TAGS = re.compile(r"<[^>]+>")
ATOM = "{http://www.w3.org/2005/Atom}"


class FeedError(ValueError):
    pass


class FeedReader:
    name = "feed"
    # Feed summaries are often a line or nothing; enrich like other thin leads.
    thin_leads = True

    def __init__(self, *, timeout: float = 30, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )

    def crawl(self, url: str, max_items: int) -> list[SearchResult]:
        response = self._client.get(url)
        response.raise_for_status()
        return parse_feed(response.content)[:max_items]


def parse_feed(content: bytes) -> list[SearchResult]:
    """Items of an RSS 2.0 or Atom feed, in feed order (usually newest first)."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as e:
        raise FeedError(f"not a feed: {e}") from e
    if root.tag == f"{ATOM}feed":
        return [_atom_entry(e) for e in root.findall(f"{ATOM}entry") if _atom_link(e)]
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise FeedError(f"not an RSS or Atom feed (root <{root.tag}>)")
    return [_rss_item(i) for i in channel.findall("item") if i.findtext("link")]


def _rss_item(item) -> SearchResult:
    return SearchResult(
        url=item.findtext("link").strip(),
        title=_text(item.findtext("title")) or item.findtext("link").strip(),
        snippet=_text(item.findtext("description")),
        published_at=(item.findtext("pubDate") or "").strip() or None,
    )


def _atom_entry(entry) -> SearchResult:
    url = _atom_link(entry)
    summary = entry.findtext(f"{ATOM}summary") or entry.findtext(f"{ATOM}content")
    published = entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")
    return SearchResult(
        url=url,
        title=_text(entry.findtext(f"{ATOM}title")) or url,
        snippet=_text(summary),
        published_at=(published or "").strip() or None,
    )


def _atom_link(entry) -> str | None:
    for link in entry.findall(f"{ATOM}link"):
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return link.get("href").strip()
    return None


def _text(value: str | None) -> str:
    """Feed text is often HTML: strip tags and entities, collapse spaces."""
    return " ".join(html.unescape(_TAGS.sub(" ", value or "")).split())[:500]
