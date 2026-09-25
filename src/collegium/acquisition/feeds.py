"""Reading RSS and Atom feeds that the owner has approved for a domain.

A feed item is a lead like a search result. Many feeds carry the whole
article (RSS content:encoded, Atom content), not just a teaser; the lead
then opens with the article itself, so it is not thin and needs no paid
extraction to be judged and quoted. Feeds are fetched as they are,
with no query, so what is sent out is only the feed's own URL. XML from
outside is parsed with defusedxml, which refuses entity-expansion attacks.
Feeds are fetched like pages (`fetching.SafeFetcher`): an approved feed
that redirects into the organization is refused, and robots.txt is obeyed.
"""

import html
import re

import httpx
from defusedxml import ElementTree

from collegium.acquisition import ENRICHED_SNIPPET_CHARS, SearchResult
from collegium.acquisition.fetching import Resolver, SafeFetcher, resolve

_TAGS = re.compile(r"<[^>]+>")
ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
# As much of the article as an enriched lead gets from its page.
LEAD_CHARS = ENRICHED_SNIPPET_CHARS
# Feeds that carry whole articles are large (METR's is about 9 MB), and
# take longer to arrive than a page.
FEED_MAX_BYTES = 20_000_000
FEED_DEADLINE_SECONDS = 120


class FeedError(ValueError):
    pass


class FeedReader:
    name = "feed"
    # Feed summaries are often a line or nothing; enrich like other thin leads.
    thin_leads = True

    def __init__(
        self,
        *,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = resolve,
    ):
        self._fetcher = SafeFetcher(timeout=timeout, transport=transport, resolver=resolver)

    def crawl(self, url: str, max_items: int) -> list[SearchResult]:
        # Any content type: feeds are often served as text/html or worse.
        _, _, body = self._fetcher.fetch(url, None, FEED_MAX_BYTES, FEED_DEADLINE_SECONDS)
        return parse_feed(body)[:max_items]


# An & that does not start an entity or character reference. Feeds are
# often written by hand or by templates that forget to escape it, and
# browsers and feed readers read them anyway.
_BARE_AMPERSAND = re.compile(rb"&(?!(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#x[0-9A-Fa-f]+);)")


def parse_feed(content: bytes) -> list[SearchResult]:
    """Items of an RSS 2.0 or Atom feed, in feed order (usually newest first).
    Bare ampersands are escaped first; defusedxml still refuses entity
    declarations, so this adds no way in."""
    try:
        root = ElementTree.fromstring(_BARE_AMPERSAND.sub(b"&amp;", content))
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
        snippet=_text(item.findtext(CONTENT) or item.findtext("description")),
        published_at=(item.findtext("pubDate") or "").strip() or None,
    )


def _atom_entry(entry) -> SearchResult:
    url = _atom_link(entry)
    summary = entry.findtext(f"{ATOM}content") or entry.findtext(f"{ATOM}summary")
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
    return " ".join(html.unescape(_TAGS.sub(" ", value or "")).split())[:LEAD_CHARS]
