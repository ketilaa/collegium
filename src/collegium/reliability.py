"""Rule-based ceilings on how reliable a source can be.

The model's reliability scores are not calibrated: in the first real run a
developer-forum complaint scored 1.00. The model still gives its score, but
it cannot rise above a ceiling set by what kind of source it comes from.
Anyone can post on a forum or a blog platform, a press release is the
claimant's own word, and a video or social post is rarely checkable.
"""

import re
from urllib.parse import urlsplit

# (source type, ceiling) by host; checked in order, most specific first.
_BY_HOST: list[tuple[tuple[str, ...], str, float]] = [
    (
        ("moltbook.com",),
        "AI-agent forum",
        0.3,
    ),
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
            "linkedin.com",
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


# Observations from these kinds of source are recorded but not researched:
# weak signals, not worth a full investigation built on them.
NOT_WORTH_RESEARCH = frozenset(["social or video", "AI-agent forum"])
# Pages of these kinds are not worth a paid read: they need a browser or
# block direct reading (Reddit, LinkedIn), and evidence from them is capped
# low anyway. What the free reader cannot read stays unread.
NOT_WORTH_PAYING = NOT_WORTH_RESEARCH | {"forum"}


def ceiling_for(uri: str) -> float:
    return classify(uri)[1]


def _host(uri: str) -> str:
    return (urlsplit(uri).hostname or "").lower().removeprefix("www.")


def site(uri: str) -> str:
    """The site a page belongs to: its host without www."""
    return _host(uri) or uri


# Sites never read at all, free or paid: adult content has no place in the
# organization's research, whatever a search engine returns (search runs
# with safe search on; this catches what gets through).
_ADULT = re.compile(
    r"porn|xnxx|xvideo|xhamster|redtube|youporn|luxuretv|brazzers|onlyfans"
    r"|(?:^|[.-])(?:xxx|sex)(?:[.-]|$)"
)


def is_blocked(uri: str) -> bool:
    return bool(_ADULT.search(_host(uri)))


# Chat apps and the like: pages built in the browser around a login, with
# no article to read, which search results often point to because a text
# mentions the product.
APP_HOSTS = (
    "chatgpt.com",
    "chat.openai.com",
    "claude.ai",
    "gemini.google.com",
    "copilot.microsoft.com",
    "perplexity.ai",
    "poe.com",
    "character.ai",
    "grok.com",
    "chat.deepseek.com",
    "chat.mistral.ai",
)


def worth_paying(uri: str) -> bool:
    """Whether a page the free reader could not read is worth a paid read.
    Not for sources whose evidence is capped low (NOT_WORTH_PAYING), for apps
    (APP_HOSTS), or for a site's front page, which is navigation, not an
    article."""
    if classify(uri)[0] in NOT_WORTH_PAYING or is_blocked(uri):
        return False
    host = _host(uri)
    if any(host == h or host.endswith("." + h) for h in APP_HOSTS):
        return False
    parts = urlsplit(uri)
    return not (parts.path in ("", "/") and not parts.query)
