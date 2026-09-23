"""Rule-based ceilings on how reliable a source can be.

The model's reliability scores are not calibrated: in the first real run a
developer-forum complaint scored 1.00. The model still gives its score, but
it cannot rise above a ceiling set by what kind of source it comes from.
Anyone can post on a forum or a blog platform, a press release is the
claimant's own word, and a video or social post is rarely checkable.
"""

from urllib.parse import urlsplit

# (source type, ceiling) by host; checked in order, most specific first.
_BY_HOST: list[tuple[tuple[str, ...], str, float]] = [
    (
        (
            "youtube.com",
            "youtu.be",
            "x.com",
            "twitter.com",
            "facebook.com",
            "tiktok.com",
            "instagram.com",
            "threads.net",
            "bsky.app",
        ),
        "social or video",
        0.3,
    ),
    (
        (
            "reddit.com",
            "news.ycombinator.com",
            "stackoverflow.com",
            "stackexchange.com",
            "quora.com",
            "discord.com",
            "discourse.org",
        ),
        "forum",
        0.4,
    ),
    (
        (
            "prnewswire.com",
            "businesswire.com",
            "globenewswire.com",
            "accesswire.com",
            "einpresswire.com",
            "prweb.com",
        ),
        "press release",
        0.5,
    ),
    (
        (
            "medium.com",
            "substack.com",
            "dev.to",
            "hashnode.dev",
            "wordpress.com",
            "blogspot.com",
            "tumblr.com",
            "linkedin.com",
        ),
        "blog platform",
        0.5,
    ),
]
# Hosts like community.openai.com or forum.example.org.
_FORUM_PREFIXES = ("community.", "forum.", "forums.", "discuss.", "discourse.")


def classify(uri: str) -> tuple[str, float]:
    """The type of source a URI belongs to, and the highest reliability
    evidence from it may have."""
    host = (urlsplit(uri).hostname or "").lower().removeprefix("www.")
    if host.startswith(_FORUM_PREFIXES):
        return "forum", 0.4
    for hosts, kind, ceiling in _BY_HOST:
        if any(host == h or host.endswith("." + h) for h in hosts):
            return kind, ceiling
    return "other", 1.0


def ceiling_for(uri: str) -> float:
    return classify(uri)[1]
