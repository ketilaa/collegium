"""Reading web pages directly, free: fetch the page, keep the article text.

The first extractor tried; pages it cannot read (blocked, built by
JavaScript, PDFs, too little text) are left to a fallback extractor such as
Tavily.

Addresses come from outside (search results, feeds), and this runs inside
the organization's network. A page must never be able to point the
organization at itself: only http(s) on the usual ports, and only hosts
that resolve to public addresses, checked again at every redirect. Pages
disallowed by the site's robots.txt are not fetched.
"""

import ipaddress
import logging
import socket
from collections.abc import Callable
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura

from collegium.acquisition import Document

log = logging.getLogger(__name__)

USER_AGENT = "Collegium/0.1 (research; read-only)"
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
# Less article text than this and the page probably needs a browser, or is
# a cookie wall; better read by the fallback.
MIN_TEXT_CHARS = 400
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

Resolver = Callable[[str], list[str]]  # host -> IP addresses


def resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


class Refused(ValueError):
    """An address the organization will not fetch."""


class WebExtractor:
    name = "web"
    metered = False

    def __init__(
        self,
        *,
        timeout: float = 20,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = resolve,
    ):
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,  # each hop is checked
            headers={"User-Agent": USER_AGENT, "Accept": ", ".join(TEXT_TYPES)},
        )
        self._resolve = resolver
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}

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

    def _fetch(self, url: str) -> tuple[str, str, bytes]:
        """The final address, content type and (decompressed) body."""
        for _ in range(MAX_REDIRECTS + 1):
            self._check(url)
            if not self._allowed(url):
                raise Refused(f"robots.txt disallows {url}")
            with self._client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind not in TEXT_TYPES:
                    return url, kind, b""  # not read at all
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise Refused(f"{url} is larger than {MAX_BYTES} bytes")
                return url, kind, body
        raise Refused(f"more than {MAX_REDIRECTS} redirects from {url}")

    def _check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise Refused(f"not a web address: {url}")
        if parts.port not in (None, 80, 443):
            raise Refused(f"unusual port in {url}")
        for address in self._resolve(parts.hostname):
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast:
                raise Refused(f"{parts.hostname} resolves to a non-public address")

    def _allowed(self, url: str) -> bool:
        """Whether robots.txt lets us read this page. A site without a
        readable robots.txt allows everything, as browsers assume."""
        parts = urlsplit(url)
        site = f"{parts.scheme}://{parts.netloc}"
        if site not in self._robots:
            self._robots[site] = self._load_robots(site)
        rules = self._robots[site]
        return rules is None or rules.can_fetch(USER_AGENT, url)

    def _load_robots(self, site: str) -> robotparser.RobotFileParser | None:
        try:
            self._check(site)
            response = self._client.get(f"{site}/robots.txt")
        except Exception:
            return None
        if response.status_code != 200:
            return None
        rules = robotparser.RobotFileParser()
        rules.parse(response.text.splitlines())
        return rules
