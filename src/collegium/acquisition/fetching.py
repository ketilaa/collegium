"""Fetching from outside addresses without being turned against ourselves.

Addresses come from outside (search results, feeds, redirects), and this
runs inside the organization's network. A page must never be able to point
the organization at itself: only http(s) on the usual ports, and only hosts
that resolve to public addresses, checked again at every redirect. What the
site's robots.txt disallows for Collegium is not fetched. Bodies are capped,
and a fetch that takes longer than its deadline, however slowly the site
sends, is given up.
"""

import ipaddress
import socket
import time
from collections.abc import Callable
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx

from collegium.identity import USER_AGENT

MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
# The whole fetch, all redirects included; httpx's timeout is per read.
DEADLINE_SECONDS = 60

Resolver = Callable[[str], list[str]]  # host -> IP addresses


def resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


class Refused(ValueError):
    """An address the organization will not fetch."""


class SafeFetcher:
    def __init__(
        self,
        *,
        timeout: float = 20,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = resolve,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,  # each hop is checked
            headers={"User-Agent": USER_AGENT},
        )
        self._resolve = resolver
        self._clock = clock
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}

    def fetch(
        self,
        url: str,
        types: tuple[str, ...] | None,
        max_bytes: int = MAX_BYTES,
        deadline: float = DEADLINE_SECONDS,
    ) -> tuple[str, str, bytes]:
        """The final address, content type and (decompressed) body. A body
        whose type is not in `types` is not read (empty); None reads any."""
        headers = {"Accept": ", ".join(types)} if types else {}
        give_up_at = self._clock() + deadline
        for _ in range(MAX_REDIRECTS + 1):
            self._on_time(url, give_up_at)
            self._check(url)
            if not self._allowed(url):
                raise Refused(f"robots.txt disallows {url}")
            with self._client.stream("GET", url, headers=headers) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if types is not None and kind not in types:
                    return url, kind, b""  # not read at all
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise Refused(f"{url} is larger than {max_bytes} bytes")
                    self._on_time(url, give_up_at)
                return url, kind, bytes(body)
        raise Refused(f"more than {MAX_REDIRECTS} redirects from {url}")

    def _on_time(self, url: str, give_up_at: float) -> None:
        if self._clock() > give_up_at:
            raise Refused(f"{url} took longer than its deadline")

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
