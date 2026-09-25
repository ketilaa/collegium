"""Reading web pages directly, free: fetch the page, keep the article text.

The first extractor tried; pages it cannot read (blocked, built by
JavaScript, PDFs, too little text) are left to a fallback extractor such as
Tavily.

Pages are fetched through `fetching.SafeFetcher`: never inside the
organization, checked at every redirect, and only where robots.txt allows.
"""

import logging
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura

from collegium.acquisition import Document, SearchResult
from collegium.acquisition.feeds import FEED_DEADLINE_SECONDS, FEED_MAX_BYTES, parse_feed
from collegium.acquisition.fetching import Resolver, SafeFetcher, resolve

log = logging.getLogger(__name__)

# Less article text than this and the page probably needs a browser, or is
# a cookie wall; better read by the fallback.
MIN_TEXT_CHARS = 400
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
FEED_TYPES = (
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
    "application/rdf+xml",
)
# Where sites usually keep their feed, when the page does not say.
FEED_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml")
_FEED_LINK = re.compile(
    r"<link\b[^>]*\btype=[\"']application/(?:rss|atom)\+xml[\"'][^>]*>", re.IGNORECASE
)
_HREF = re.compile(r"\bhref=[\"']([^\"']+)[\"']", re.IGNORECASE)
# A feed worth proposing has at least this many items.
MIN_FEED_ITEMS = 3
# A page may announce any number of feeds, anywhere; only the first few are
# tried, and all candidates share one deadline.
MAX_ANNOUNCED_FEEDS = 3


class WebExtractor:
    name = "web"
    metered = False

    def __init__(
        self,
        *,
        timeout: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = resolve,
    ):
        self._fetcher = SafeFetcher(timeout=timeout, transport=transport, resolver=resolver)

    def extract(self, urls: list[str]) -> list[Document]:
        """Documents for the pages that could be read; the rest are left out."""
        documents = []
        for url in urls:
            try:
                document = self._read(url)
            except Exception as e:  # one unreadable page must not stop the others
                log.info("could not read %s: %s: %s", url, type(e).__name__, e)
                continue
            if document is not None:
                documents.append(document)
        return documents

    def _read(self, url: str) -> Document | None:
        final_url, kind, body = self._fetch(url)
        if kind not in TEXT_TYPES:
            return None
        # trafilatura detects the page's encoding itself.
        text = trafilatura.extract(body, url=final_url, include_comments=False) or ""
        if len(text) < MIN_TEXT_CHARS:
            return None
        meta = trafilatura.extract_metadata(body, default_url=final_url)
        return Document(
            url=url,  # the address asked for, which the lead and citations use
            title=(meta.title if meta and meta.title else "") or url,
            content=text,
            published_at=meta.date if meta and meta.date else None,
            metadata={"fetched_from": final_url} if final_url != url else {},
        )

    def find_feed(self, site_url: str) -> list[SearchResult]:
        """The feed of a site, as a one-item list naming it (url, title, and
        how many items it has), or empty if none was found. Looks for the
        feed the page announces, then at the usual places, and checks that
        it parses and has items. Fetched with the same protections as pages,
        and given up when the candidates together take FEED_DEADLINE_SECONDS."""
        give_up_at = time.monotonic() + FEED_DEADLINE_SECONDS
        candidates: list[str] = []
        try:
            final, kind, body = self._fetch(site_url)
            if kind in TEXT_TYPES:
                head = body[:200_000].decode("utf-8", "replace")
                announced = [
                    urljoin(final, m.group(1))
                    for link in _FEED_LINK.findall(head)
                    if (m := _HREF.search(link))
                ]
                candidates += list(dict.fromkeys(announced))[:MAX_ANNOUNCED_FEEDS]
        except Exception as e:
            log.info("could not read %s: %s", site_url, e)
        root = "{0.scheme}://{0.netloc}".format(urlsplit(site_url))
        candidates += [root + path for path in FEED_PATHS]
        for url in dict.fromkeys(candidates):
            remaining = give_up_at - time.monotonic()
            if remaining <= 0:
                log.info("gave up looking for the feed of %s", site_url)
                break
            try:
                final, kind, body = self._fetcher.fetch(
                    url, FEED_TYPES + TEXT_TYPES, FEED_MAX_BYTES, remaining
                )
                items = parse_feed(body)
            except Exception:
                continue
            if len(items) >= MIN_FEED_ITEMS:
                return [
                    SearchResult(
                        url=final,
                        title=_feed_title(body) or final,
                        snippet=f"{len(items)} items",
                        metadata={"items": len(items), "newest": items[0].published_at},
                    )
                ]
        return []

    def _fetch(self, url: str, types: tuple[str, ...] = TEXT_TYPES) -> tuple[str, str, bytes]:
        return self._fetcher.fetch(url, types)


def _feed_title(body: bytes) -> str | None:
    """The feed's own title, if it has one."""
    m = re.search(rb"<title[^>]*>(.*?)</title>", body[:20_000], re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    title = m.group(1).decode("utf-8", "replace").replace("<![CDATA[", "").replace("]]>", "")
    return " ".join(title.split())[:120] or None
