"""Web search through SearXNG, a metasearch engine the organization runs
itself (the `searxng` Compose service): free and without a quota.

SearXNG asks several engines at once and merges their results. Results are
leads with a short snippet; the acquisition layer enriches the best of them
from their pages, which the free web extractor reads.
"""

import time

import httpx

from collegium.acquisition import SearchResult, SearchUnavailable
from collegium.identity import USER_AGENT

# Seconds between queries. The engines behind SearXNG block an address that
# asks too fast, and the organization is not in a hurry.
PAUSE = 3.0


def time_range(recent_days: int | None) -> str | None:
    """SearXNG's coarse time filter that covers `recent_days`."""
    if not recent_days:
        return None
    for days, name in ((1, "day"), (7, "week"), (31, "month")):
        if recent_days <= days:
            return name
    return "year"


class SearXNGDiscovery:
    name = "searxng"
    thin_leads = True

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
        pause: float = PAUSE,
    ):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": USER_AGENT},
        )
        self._pause = pause
        self._last = float("-inf")

    def discover(
        self, query: str, max_results: int, *, recent_days: int | None = None
    ) -> list[SearchResult]:
        params = {"q": query, "format": "json", "language": "all", "safesearch": 0}
        if window := time_range(recent_days):
            params["time_range"] = window
        wait = self._last + self._pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            response = self._client.get("/search", params=params)
        finally:
            self._last = time.monotonic()
        response.raise_for_status()
        body = response.json()
        results = []
        for hit in body.get("results", []):
            url = hit.get("url") or ""
            if not url.startswith(("http://", "https://")) or not hit.get("title"):
                continue
            results.append(
                SearchResult(
                    url=url,
                    title=hit["title"],
                    snippet=hit.get("content") or "",
                    published_at=hit.get("publishedDate"),
                    score=hit.get("score"),
                    metadata={"engines": hit.get("engines", [])},
                )
            )
            if len(results) == max_results:
                break
        # Nothing found while engines refused to answer is an outage, not an
        # empty result: say so, so that no paid search stands in for it.
        refused = body.get("unresponsive_engines") or []
        if not results and refused:
            raise SearchUnavailable(
                "search engines unavailable: "
                + ", ".join(f"{name} ({why})" for name, why in refused)
            )
        return results
