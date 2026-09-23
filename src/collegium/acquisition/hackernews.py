"""Hacker News discovery, through the Algolia HN Search API (free, no key).

A story is a lead, not a source: its url is the linked article, which is
what gets extracted and cited. The discussion link and attention signals
(points, comments) travel along as metadata. Text posts (Ask HN, Show HN)
have no link, so the discussion itself is the lead.
"""

import html
import re
import time

import httpx

from collegium.acquisition import SearchResult

API = "https://hn.algolia.com/api/v1"
DISCUSSION = "https://news.ycombinator.com/item?id={}"
_TAGS = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[\w.+#'-]+")
# Words that carry no topic. HN search matches keywords, and a query such as
# "recent developments in AI models" otherwise finds nothing.
_FILLER = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "latest",
        "new",
        "news",
        "of",
        "on",
        "or",
        "recent",
        "recently",
        "the",
        "their",
        "to",
        "trends",
        "what",
        "what's",
        "which",
        "who",
        "why",
        "with",
    ]
)


# HN's value as a source is attention: what practitioners upvote. Stories
# below this have not been noticed yet and are mostly noise.
MIN_POINTS = 30


def keywords(query: str) -> str:
    words = [w for w in _WORD.findall(query.lower()) if w not in _FILLER]
    return " ".join(words) or query


class HackerNewsDiscovery:
    name = "hackernews"
    # Leads are a title and a score; the acquisition layer enriches them.
    thin_leads = True

    def __init__(
        self,
        *,
        min_points: int = MIN_POINTS,
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
    ):
        self._min_points = min_points
        self._client = httpx.Client(base_url=API, timeout=timeout, transport=transport)

    def discover(
        self, query: str, max_results: int, *, recent_days: int | None = None
    ) -> list[SearchResult]:
        terms = keywords(query)
        # Optional words: a story matching most terms still counts, ranked by
        # how many it matches.
        params: dict[str, str | int] = {
            "query": terms,
            "optionalWords": terms,
            "tags": "story",
            "hitsPerPage": max_results,
        }
        filters = [f"points>={self._min_points}"]
        if recent_days:
            filters.append(f"created_at_i>{int(time.time()) - recent_days * 86400}")
        params["numericFilters"] = ",".join(filters)
        response = self._client.get("/search", params=params)
        response.raise_for_status()
        return [_lead(hit) for hit in response.json().get("hits", []) if hit.get("title")]


def _lead(hit: dict) -> SearchResult:
    discussion = DISCUSSION.format(hit["objectID"])
    points, comments = hit.get("points") or 0, hit.get("num_comments") or 0
    signal = f"Hacker News: {points} points, {comments} comments"
    text = " ".join(html.unescape(_TAGS.sub(" ", hit.get("story_text") or "")).split())
    return SearchResult(
        url=hit.get("url") or discussion,
        title=hit["title"],
        snippet=f"{text[:500]}\n{signal}" if text else signal,
        published_at=hit.get("created_at"),
        metadata={"hn_discussion": discussion, "hn_points": points, "hn_comments": comments},
    )
