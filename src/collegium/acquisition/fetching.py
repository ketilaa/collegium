"""Fetching from outside addresses without being turned against ourselves.

Addresses come from outside (search results, feeds, redirects), and this
runs inside the organization's network. A page must never be able to point
the organization at itself: only http(s) on the usual ports, and only hosts
that resolve to public addresses, checked again at every redirect. The
connection goes to an address that was checked, never to a second lookup
of the name, which a hostile DNS server could answer differently (DNS
rebinding). What the site's robots.txt disallows for Collegium is not
fetched, and a site whose robots.txt cannot be read is not fetched either.
Bodies are capped.

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

import httpcore
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


# robots.txt that could not be read (a server error, a timeout): nothing on
# the site is fetched this time, as RFC 9309 asks, and it is asked again.
UNREACHABLE = object()
MAX_ROBOTS_REDIRECTS = 5


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Connects only to addresses `_check` approved for the host, never to
    what a fresh lookup of the name returns. TLS still uses the name (SNI
    and the certificate), and so does the Host header: httpcore takes both
    from the URL, not from the address connected to."""

    def __init__(self, pins: dict[str, list[str]], inner: httpcore.AsyncNetworkBackend):
        self._pins = pins
        self._inner = inner

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = self._pins.get(host)
        if not addresses:
            raise httpcore.ConnectError(f"{host} was not checked")
        error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                error = e
        raise error

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("no sockets on this machine")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _pinned_transport(
    pins: dict[str, list[str]], inner: httpcore.AsyncNetworkBackend
) -> httpx.AsyncHTTPTransport:
    """httpx's own transport, with its connection pool connecting through
    PinnedBackend (httpx does not take a network backend itself)."""
    transport = httpx.AsyncHTTPTransport()
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(),
        network_backend=PinnedBackend(pins, inner),
    )
    return transport


class SafeFetcher:
    def __init__(
        self,
        *,
        timeout: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = resolve,
        network: httpcore.AsyncNetworkBackend | None = None,
    ):
        # `transport` replaces the network entirely (tests); otherwise every
        # connection goes through `network` (default: sockets), pinned to
        # the checked addresses.
        self._timeout = timeout
        self._transport = transport
        self._resolve = resolver
        self._network = network or httpcore.AnyIOBackend()
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
        pins: dict[str, list[str]] = {}  # host -> the addresses checked for it
        try:
            async with asyncio.timeout(deadline):
                async with httpx.AsyncClient(
                    timeout=self._timeout,
                    transport=self._transport or _pinned_transport(pins, self._network),
                    follow_redirects=False,  # each hop is checked
                    headers={"User-Agent": USER_AGENT},
                ) as client:
                    return await self._fetch(client, pins, url, types, max_bytes)
        except TimeoutError:
            raise Refused(f"{url} took longer than {deadline} seconds") from None

    async def _fetch(self, client, pins, url, types, max_bytes) -> tuple[str, str, bytes]:
        headers = {"Accept": ", ".join(types)} if types else {}
        for _ in range(MAX_REDIRECTS + 1):
            await self._check(url, pins)
            if not await self._allowed(client, pins, url):
                raise Refused(f"robots.txt disallows {url}, or could not be read")
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

    async def _check(self, url: str, pins: dict[str, list[str]]) -> None:
        """Refuse anything but http(s) on the usual ports to public addresses,
        and pin the host to the addresses checked: the connection goes to
        one of them, never to a second lookup."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise Refused(f"not a web address: {url}")
        if parts.port not in (None, 80, 443):
            raise Refused(f"unusual port in {url}")
        loop = asyncio.get_running_loop()
        addresses = await loop.run_in_executor(_LOOKUPS, self._resolve, parts.hostname)
        if not addresses:
            raise Refused(f"{parts.hostname} does not resolve")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast:
                raise Refused(f"{parts.hostname} resolves to a non-public address")
        pins[parts.hostname] = addresses

    async def _allowed(self, client: httpx.AsyncClient, pins: dict, url: str) -> bool:
        """Whether robots.txt lets us read this page (RFC 9309): no
        robots.txt (4xx) allows everything; one that cannot be read (5xx,
        errors, timeouts) allows nothing this time and is asked again."""
        parts = urlsplit(url)
        site = f"{parts.scheme}://{parts.netloc}"
        rules = self._robots.get(site, UNREACHABLE)
        if site not in self._robots:
            rules = await self._load_robots(client, pins, site)
            if rules is not UNREACHABLE:
                self._robots[site] = rules
        if rules is UNREACHABLE:
            return False
        return rules is None or rules.can_fetch(USER_AGENT, url)

    async def _load_robots(self, client: httpx.AsyncClient, pins: dict, site: str):
        """The site's rules, read no further than ROBOTS_MAX_BYTES; None when
        it has none; UNREACHABLE when they could not be read. Redirects are
        followed, each hop checked like any other. A robots.txt that trickles
        in is not waited for beyond the fetch's deadline, which cancels this
        too (CancelledError is not an Exception)."""
        url = f"{site}/robots.txt"
        try:
            for _ in range(MAX_ROBOTS_REDIRECTS + 1):
                await self._check(url, pins)
                async with client.stream("GET", url) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    if 400 <= response.status_code < 500:
                        return None
                    if response.status_code != 200:
                        return UNREACHABLE
                    body = await _read(response, ROBOTS_MAX_BYTES, truncate=True)
                    text = body.decode(response.encoding or "utf-8", "replace")
                    break
            else:
                return UNREACHABLE
        except Exception:  # refused hops and errors alike
            return UNREACHABLE
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
