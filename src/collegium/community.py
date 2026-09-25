"""The community agent on Moltbook: what a post or a reply may be.

Stage 2 of the community agent (docs/decisions.md). The Researcher drafts
posts (questions about hypotheses) and replies (answers to other agents from
what memory holds), the owner approves each one (and may edit it) on the
board, and the publisher, a separate component that alone holds the key,
publishes it. These rules are shared by all three, so what reaches the
forum is exactly what the owner saw.
"""

import re

FORUM = "moltbook"
SITE = "https://www.moltbook.com"

# Forums the organization may post in, with what each is for. A draft names
# one; the owner can choose another when approving.
SUBMOLTS = {
    "agents": "autonomous agents: workflows, architectures, how agents work",
    "ai-agents": "building, running and thinking about AI agents",
    "memory": "the agent memory problem: systems and strategies",
    "multiagent": "multi-agent coordination, delegation and orchestration",
    "ai": "AI news, research, tools and discussion",
    "ai-coding": "AI-assisted code generation, review and refactoring",
    "coding": "practical coding, debugging, tooling and shipping software",
    "programming": "code, development and software engineering",
    "research": "tools for scientific research",
    "aisafety": "alignment, security, trust and the risks of AI",
    "technology": "technology news and infrastructure",
    "general": "anything that fits nowhere else",
}
DEFAULT_SUBMOLT = "ai"

MAX_TITLE = 200
MAX_CONTENT = 6000  # well within the forum's limit; a post is a question, not a report
MAX_COMMENT = 3000

# Every post says who wrote it. That the owner approved it is not said:
# everything posted is approved (the owner's decision).
SIGNATURE = (
    "\n\n---\n"
    "Posted by drargus, the community agent of Collegium, a research organization "
    "with institutional memory."
)
REPLY_SIGNATURE = "\n\n— drargus, for Collegium"

_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def without_links(text: str) -> str:
    """Model-written text with any address removed: the only links in a post
    are the sources code adds from memory."""
    return "\n".join(" ".join(_URL.sub("", line).split()) for line in text.splitlines()).strip()


def compose(body: str, question: str, sources: list[str]) -> str:
    """The post's text: what the organization holds, its question, the
    sources it rests on, and the signature."""
    parts = [body.strip(), f"**Our question:** {question.strip()}"]
    if sources:
        parts.append("Sources we rely on:\n" + "\n".join(f"- {url}" for url in sources))
    return "\n\n".join(parts) + SIGNATURE


def compose_reply(points: list[str], sources: list[str]) -> str:
    """A reply's text: what memory holds, point by point, and its sources."""
    parts = ["\n\n".join(p.strip() for p in points if p.strip())]
    if sources:
        parts.append("Sources:\n" + "\n".join(f"- {url}" for url in sources))
    return "\n\n".join(parts) + REPLY_SIGNATURE


_THREAD = re.compile(
    r"^https://www\.moltbook\.com/post/([A-Za-z0-9-]+)(?:#comment-([A-Za-z0-9-]+))?$"
)


def thread(url: str) -> tuple[str, str | None] | None:
    """The post id and, for a comment, its id, from a Moltbook address as
    the organization records it; None for anything else."""
    m = _THREAD.match(url or "")
    return (m.group(1), m.group(2)) if m else None


def reply_problems(content: str) -> list[str]:
    if not content.strip():
        return ["A reply needs text."]
    if len(content) > MAX_COMMENT:
        return [f"The reply is longer than {MAX_COMMENT} characters."]
    return []


def problems(title: str, content: str, submolt: str) -> list[str]:
    """What is wrong with a post as the owner would send it, if anything."""
    found = []
    if not title.strip():
        found.append("A post needs a title.")
    elif len(title) > MAX_TITLE:
        found.append(f"The title is longer than {MAX_TITLE} characters.")
    if not content.strip():
        found.append("A post needs text.")
    elif len(content) > MAX_CONTENT:
        found.append(f"The text is longer than {MAX_CONTENT} characters.")
    if submolt not in SUBMOLTS:
        found.append(f"m/{submolt} is not a forum the organization posts in.")
    return found


def post_url(post_id: str) -> str:
    return f"{SITE}/post/{post_id}"
