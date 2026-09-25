"""Fetching from outside addresses without being turned against ourselves.

Addresses come from outside (search results, feeds, redirects), and this
runs inside the organization's network. A page must never be able to point
the organization at itself: only http(s) on the usual ports, and only hosts
that resolve to public addresses, checked again at every redirect. What the
site's robots.txt disallows for Collegium is not fetched. Bodies are capped.

Each fetch has a deadline for the whole of it: the name lookups, robots.txt,
every redirect, the handshake, the headers and the body. httpx's own timeout
is per read, so a site sending a byte now and then would never trip it; the
fetch runs under `asyncio.timeout` instead, which cancels wherever it waits.
From outside it is an ordinary blocking call.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx

from collegium.identity import USER_AGENT

MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
DEADLINE_SECONDS = 60
# Rules past this point are ignored, as Google does with its 500 KiB.
ROBOTS_MAX_BYTES = 500_000

Resolver = Callable[[str], list[str]]  # host -> IP addresses

# Name lookups block and have no timeout of their own, so they run in
# threads the deadline need not wait for (asyncio.run waits for its own).
_LOOKUPS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lookup")


def resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


class Refused(ValueError):
    """An address the organization will not fetch."""


class SafeFetcher:
    def __init__(
        self,
        *,
        timeout: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = resolve,
    ):
        self._timeout = timeout
        self._transport = transport
        self._resolve = resolver
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}

    def fetch(
        self,
        url: str,
        types: tuple[str, ...] | None,
        max_bytes: int = MAX_BYTES,
        deadline: float = DEADLINE_SECONDS,
    ) -> tuple[str, str, bytes]:
        """The final address, content type and (decompressed) body. A body
        whose type is not in `types` is not read (empty); None reads any.
        Refused if it is not done within `deadline` seconds."""
        return asyncio.run(self._within(url, types, max_bytes, deadline))

    async def _within(self, url, types, max_bytes, deadline) -> tuple[str, str, bytes]:
        try:
            async with asyncio.timeout(deadline):
                async with httpx.AsyncClient(
                    timeout=self._timeout,
                    transport=self._transport,
                    follow_redirects=False,  # each hop is checked
                    headers={"User-Agent": USER_AGENT},
                ) as client:
                    return await self._fetch(client, url, types, max_bytes)
        except TimeoutError:
            raise Refused(f"{url} took longer than {deadline} seconds") from None

    async def _fetch(self, client, url, types, max_bytes) -> tuple[str, str, bytes]:
        headers = {"Accept": ", ".join(types)} if types else {}
        for _ in range(MAX_REDIRECTS + 1):
            await self._check(url)
            if not await self._allowed(client, url):
                raise Refused(f"robots.txt disallows {url}")
            async with client.stream("GET", url, headers=headers) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if types is not None and kind not in types:
                    return url, kind, b""  # not read at all
                body = await _read(response, max_bytes)
                if body is None:
                    raise Refused(f"{url} is larger than {max_bytes} bytes")
                return url, kind, body
        raise Refused(f"more than {MAX_REDIRECTS} redirects from {url}")

    async def _check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise Refused(f"not a web address: {url}")
        if parts.port not in (None, 80, 443):
            raise Refused(f"unusual port in {url}")
        loop = asyncio.get_running_loop()
        for address in await loop.run_in_executor(_LOOKUPS, self._resolve, parts.hostname):
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast:
                raise Refused(f"{parts.hostname} resolves to a non-public address")

    async def _allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        """Whether robots.txt lets us read this page. A site without a
        readable robots.txt allows everything, as browsers assume."""
        parts = urlsplit(url)
        site = f"{parts.scheme}://{parts.netloc}"
        if site not in self._robots:
            self._robots[site] = await self._load_robots(client, site)
        rules = self._robots[site]
        return rules is None or rules.can_fetch(USER_AGENT, url)

    async def _load_robots(
        self, client: httpx.AsyncClient, site: str
    ) -> robotparser.RobotFileParser | None:
        """The site's rules, read no further than ROBOTS_MAX_BYTES. A robots.txt
        that trickles in is not waited for beyond the fetch's deadline, which
        cancels this too (CancelledError is not an Exception)."""
        try:
            await self._check(site)
            async with client.stream("GET", f"{site}/robots.txt") as response:
                if response.status_code != 200:
                    return None
                body = await _read(response, ROBOTS_MAX_BYTES, truncate=True)
                text = body.decode(response.encoding or "utf-8", "replace")
        except Exception:
            return None
        rules = robotparser.RobotFileParser()
        rules.parse(text.splitlines())
        return rules


async def _read(response: httpx.Response, max_bytes: int, truncate: bool = False) -> bytes | None:
    """The body, or None when it is larger than `max_bytes` (with `truncate`,
    its first `max_bytes` instead)."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > max_bytes:
            return bytes(body[:max_bytes]) if truncate else None
    return bytes(body)
