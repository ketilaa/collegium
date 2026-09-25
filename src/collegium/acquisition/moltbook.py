"""Moltbook, a forum where AI agents post: discovery through its search.

Reads need no API key, so this adapter has none and no way to write
anything: the key is held only by the publisher (`collegium.publisher`),
which sends the posts the owner approved. Replies to those posts come back
through here, as leads like any other.

Everything here is written by other AI agents: unverified, often
repetitive, and a natural channel for prompt injection. Leads carry the
source type "AI-agent forum", which caps their reliability, keeps them from
paid reading, and keeps observations from them out of research.
"""

import httpx

from collegium.acquisition import ENRICHED_SNIPPET_CHARS, SearchResult
from collegium.identity import USER_AGENT

# The community agent's own account: its words are not leads.
OWN_ACCOUNT = "drargus"

API = "https://www.moltbook.com/api/v1"
SITE = "https://www.moltbook.com"


class MoltbookDiscovery:
    name = "moltbook"

    def __init__(self, *, timeout: float = 30, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=API,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": USER_AGENT},
        )

    def discover(
        self, query: str, max_results: int, *, recent_days: int | None = None
    ) -> list[SearchResult]:
        """Posts matching the query, each read in full from the API (search
        returns only the start of a post, and the site needs a browser)."""
        # Search also returns agent profiles unless asked for posts only.
        response = self._client.get(
            "/search", params={"q": query, "type": "posts", "limit": max_results * 2}
        )
        response.raise_for_status()
        leads = []
        for hit in response.json().get("results", []):
            if hit.get("type") != "post" or not hit.get("post_id"):
                continue
            post = self._post(hit["post_id"])
            if post is None or post.get("is_spam") or post.get("is_deleted"):
                continue
            leads.append(_lead(post))
            if len(leads) == max_results:
                break
        return leads

    def replies(self, post_id: str, max_items: int) -> list[SearchResult]:
        """Comments on one of the organization's posts, newest first,
        replies to comments included; its own comments left out."""
        response = self._client.get(
            f"/posts/{post_id}/comments", params={"sort": "new", "limit": max_items}
        )
        response.raise_for_status()
        leads: list[SearchResult] = []
        pending = list(response.json().get("comments", []))
        while pending and len(leads) < max_items:
            comment = pending.pop(0)
            pending += comment.get("replies") or []
            author = (comment.get("author") or {}).get("name") or "unknown agent"
            text = " ".join((comment.get("content") or "").split())
            if author == OWN_ACCOUNT or not text or comment.get("is_deleted"):
                continue
            leads.append(
                SearchResult(
                    url=f"{SITE}/post/{post_id}#comment-{comment['id']}",
                    title=f"Reply by agent {author} to Collegium's question",
                    snippet=f"{text[:ENRICHED_SNIPPET_CHARS]}\nMoltbook comment by agent "
                    f"{author}: {comment.get('upvotes', 0)} upvotes",
                    published_at=comment.get("created_at"),
                    metadata={"moltbook_author": author, "moltbook_reply_to": post_id},
                )
            )
        return leads

    def _post(self, post_id: str) -> dict | None:
        response = self._client.get(f"/posts/{post_id}")
        if response.status_code != 200:
            return None
        body = response.json()
        return body.get("post", body)


def _lead(post: dict) -> SearchResult:
    author = (post.get("author") or {}).get("name") or "unknown agent"
    submolt = (post.get("submolt") or {}).get("name") or ""
    signal = (
        f"Moltbook post by agent {author} in m/{submolt}: "
        f"{post.get('upvotes', 0)} upvotes, {post.get('comment_count', 0)} comments"
    )
    text = " ".join((post.get("content") or "").split())[:ENRICHED_SNIPPET_CHARS]
    return SearchResult(
        url=f"{SITE}/post/{post['id']}",
        title=post.get("title") or text[:80],
        snippet=f"{text}\n{signal}",
        published_at=post.get("created_at"),
        metadata={"moltbook_author": author, "moltbook_submolt": submolt},
    )
