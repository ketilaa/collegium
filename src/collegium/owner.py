"""What the owner can do, shared by the CLI and the web board.

Every function runs inside a transaction acting as the owner
(`Database.acting_as("owner")`, on a board login), so the database records
the owner as the author of each change. Problems the owner can fix raise
OwnerError with a message meant for them.
"""

from uuid import UUID

import psycopg

from collegium import community, jobs, memory
from collegium.acquisition.moltbook import OWN_ACCOUNT
from collegium.db import Connection

DOMAIN_STATUSES = ("active", "paused", "retired")
FEED_STATUSES = ("active", "paused", "retired")
REQUESTS = ("scout", "strategize")  # work the owner can ask for now


class OwnerError(Exception):
    """A request the owner can correct: a bad value, or nothing to act on."""


def resolve_decision(
    conn: Connection,
    decision_id: UUID,
    status: str,
    reason: str | None = None,
    *,
    post: dict | None = None,
) -> str | None:
    """Approve or reject a proposed decision. A rejection's reason is kept
    as the owner's upheld critique of the proposal. A decision about a
    program opens or closes it; returns the program's name if so. Approving
    a post puts it in the publisher's outbox, with the owner's edits
    (`post`: title, content, submolt) if any."""
    if status not in ("approved", "rejected"):
        raise OwnerError(f"unknown decision status {status!r}")
    updated = conn.execute(
        "UPDATE decisions SET status = %s, resolved_by = current_actor(), resolved_at = now() "
        "WHERE id = %s AND status = 'proposed'",
        (status, decision_id),
    ).rowcount
    if not updated:
        raise OwnerError("That decision is no longer waiting for you.")
    reason = (reason or "").strip()
    if status == "rejected" and reason:
        critique_id = memory.add_critique(
            conn, target_id=decision_id, argument=reason, alternative_explanation=None, severity=3
        )
        memory.set_critique_status(
            conn, critique_id, "upheld", "The owner's reason for rejecting the proposal."
        )
    decision = conn.execute(
        "SELECT topic, details FROM decisions WHERE id = %s", (decision_id,)
    ).fetchone()
    if status == "approved" and decision["topic"] == "source":
        details = decision["details"]
        conn.execute(
            "INSERT INTO approved_sources (domain_id, kind, url, title) "
            "VALUES (%s, 'feed', %s, %s) ON CONFLICT (domain_id, url) DO NOTHING",
            (details["domain_id"], details["feed_url"], details.get("title")),
        )
    if status == "approved" and decision["topic"] == "post":
        _queue_post(conn, decision_id, {**decision["details"], **(post or {})})
    if status == "approved" and decision["topic"] == "reply":
        _queue_reply(conn, decision_id, {**decision["details"], **(post or {})})
    program_status = "active" if status == "approved" else "closed"
    program = conn.execute(
        "UPDATE programs SET status = %s WHERE id IN (SELECT object_id FROM relationships "
        "WHERE subject_id = %s AND predicate = 'concerns' AND retracted_at IS NULL) "
        "RETURNING name",
        (program_status, decision_id),
    ).fetchone()
    return program["name"] if program else None


def _queue_post(conn: Connection, decision_id: UUID, details: dict) -> None:
    """The approved post, exactly as the owner last saw it, into the outbox."""
    title = (details.get("title") or "").strip()
    content = (details.get("content") or "").replace("\r\n", "\n").strip()
    submolt = (details.get("submolt") or "").strip()
    if found := community.problems(title, content, submolt):
        raise OwnerError(" ".join(found))
    conn.execute(
        "INSERT INTO community_posts (decision_id, domain_id, about_id, submolt, title, content) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            decision_id,
            details.get("domain_id"),
            details.get("hypothesis_id"),
            submolt,
            title,
            content,
        ),
    )


def _queue_reply(conn: Connection, decision_id: UUID, details: dict) -> None:
    """The approved reply, exactly as the owner last saw it, into the outbox."""
    content = (details.get("content") or "").replace("\r\n", "\n").strip()
    if found := community.reply_problems(content):
        raise OwnerError(" ".join(found))
    conn.execute(
        "INSERT INTO community_posts (decision_id, domain_id, about_id, kind, reply_to_post, "
        "reply_to_comment, content) VALUES (%s, %s, %s, 'comment', %s, %s, %s)",
        (
            decision_id,
            details.get("domain_id"),
            details.get("observation_id"),
            details["reply_to_post"],
            details.get("reply_to_comment"),
            content,
        ),
    )
    # An acknowledgement ("we will look into ...") becomes research only now,
    # with the owner's approval: the subject was raised by another agent.
    subject = details.get("look_into")
    if subject and details.get("domain_id"):
        queued = conn.execute(
            "SELECT 1 FROM jobs WHERE kind = 'scout' AND status IN ('pending', 'running') "
            "AND payload ->> 'focus' = %s",
            (subject,),
        ).fetchone()
        if not queued:
            jobs.enqueue(
                conn,
                "scout",
                {"domain_id": details["domain_id"], "focus": subject, "focus_from": "moltbook"},
            )
    # Answering a comment is also a vote for it.
    _queue_vote(
        conn,
        details["reply_to_post"],
        details.get("reply_to_comment"),
        domain_id=details.get("domain_id"),
        about_id=details.get("observation_id"),
        why="Upvoted with the reply to it.",
    )


def _queue_vote(
    conn: Connection,
    post_id: str,
    comment_id: str | None,
    *,
    domain_id=None,
    about_id=None,
    why: str,
) -> bool:
    """An upvote on a Moltbook post or comment into the outbox, as a
    decision the owner takes by doing this. False if already voted for."""
    voted = conn.execute(
        "SELECT 1 FROM community_posts WHERE kind = 'vote' AND reply_to_post = %s "
        "AND coalesce(reply_to_comment, '') = coalesce(%s, '') "
        "AND status NOT IN ('failed', 'withdrawn')",
        (post_id, comment_id),
    ).fetchone()
    if voted:
        return False
    target = community.post_url(post_id) + (f"#comment-{comment_id}" if comment_id else "")
    decision_id = memory.add_decision(
        conn,
        statement=f"Upvote on Moltbook: {target}",
        rationale=why,
        topic="vote",
        details={
            "forum": community.FORUM,
            "reply_to_post": post_id,
            "reply_to_comment": comment_id,
        },
        decided=True,
    )
    conn.execute(
        "INSERT INTO community_posts (decision_id, domain_id, about_id, kind, reply_to_post, "
        "reply_to_comment) VALUES (%s, %s, %s, 'vote', %s, %s)",
        (decision_id, domain_id, about_id, post_id, comment_id),
    )
    return True


def upvote(conn: Connection, url: str) -> None:
    """The owner's upvote for a Moltbook post or comment the organization
    has recorded (nothing else can be voted for from the board)."""
    where = community.thread(url)
    if where is None:
        raise OwnerError("Only a Moltbook post or comment can be upvoted.")
    source = conn.execute("SELECT metadata FROM sources WHERE uri = %s LIMIT 1", (url,)).fetchone()
    if source is None:
        raise OwnerError("The organization has not recorded that post or comment.")
    own = (source["metadata"] or {}).get("moltbook_author") == OWN_ACCOUNT
    if own or (where[1] is None and memory.own_post(conn, where[0])):
        raise OwnerError("The organization does not vote for its own posts or comments.")
    if not _queue_vote(conn, where[0], where[1], why="The owner's upvote."):
        raise OwnerError("That already has our vote.")


def request_reply(conn: Connection, observation_id: UUID) -> UUID:
    """Ask the Researcher whether memory has something to say to a Moltbook
    post or comment, and to draft a reply if so. It comes back as a
    decision, or not at all when memory has nothing to add."""
    o = memory.observation(conn, observation_id)
    if o is None:
        raise OwnerError("No such observation.")
    if community.thread(o["source_uri"] or "") is None:
        raise OwnerError("Only a Moltbook post or comment can be replied to.")
    if memory.post_decision(conn, observation_id, ("proposed", "approved"), "reply"):
        raise OwnerError("A reply to it has already been drafted.")
    return jobs.enqueue(conn, "reply", {"observation_id": observation_id}, priority=2)


def withdraw_program(conn: Connection, program_id: UUID) -> str:
    """Close a program the owner already opened: it stops guiding new
    goals, but its history (decisions and goals already linked to it)
    stays on record. For a program that turns out to duplicate another,
    or no longer deserves the attention."""
    program = conn.execute(
        "UPDATE programs SET status = 'closed' WHERE id = %s AND status IN ('active', 'paused') "
        "RETURNING name",
        (program_id,),
    ).fetchone()
    if not program:
        raise OwnerError("That program is not open.")
    return program["name"]


def withdraw_post(conn: Connection, post_id: UUID) -> None:
    """Take an approved post back before the publisher sends it."""
    updated = conn.execute(
        "UPDATE community_posts SET status = 'withdrawn' WHERE id = %s AND status = 'approved'",
        (post_id,),
    ).rowcount
    if not updated:
        raise OwnerError("That post is no longer waiting to be published.")


def retry_post(conn: Connection, post_id: UUID) -> None:
    """Send a failed post, reply or vote back for another attempt: often a
    one-off verification miss, not a reason to give up on approved
    content. Moltbook's own advice on a failed challenge is to send the
    content again, so this is a fresh attempt, not a re-verification of
    the old one; the record of what the failed attempt did stays until the
    publisher overwrites it with the new outcome."""
    updated = conn.execute(
        "UPDATE community_posts SET status = 'approved', error = NULL "
        "WHERE id = %s AND status = 'failed'",
        (post_id,),
    ).rowcount
    if not updated:
        raise OwnerError("That post has not failed, or no longer exists.")


def request_draft(conn: Connection, hypothesis_id: UUID) -> UUID:
    """Ask the Researcher to draft a Moltbook post about a hypothesis. It
    comes back as a decision: nothing is posted without the owner."""
    if memory.hypothesis(conn, hypothesis_id) is None:
        raise OwnerError("No such hypothesis.")
    if memory.post_decision(conn, hypothesis_id, ("proposed",)):
        raise OwnerError("A draft about it is already waiting for you under Decisions.")
    return jobs.enqueue(conn, "draft", {"hypothesis_id": hypothesis_id}, priority=2)


def challenge(
    conn: Connection,
    hypothesis_id: UUID,
    argument: str,
    *,
    alternative: str | None = None,
    severity: int = 3,
) -> UUID:
    """The owner's critique of a hypothesis. It goes through the critique
    loop like the Skeptic's, starting a fresh two rounds: the Researcher
    investigates it and the Skeptic settles it."""
    argument = argument.strip()
    if not argument:
        raise OwnerError("Say what you object to.")
    if not 1 <= severity <= 5:
        raise OwnerError("Severity is from 1 to 5.")
    h = memory.hypothesis(conn, hypothesis_id)
    if h is None:
        raise OwnerError("No such hypothesis.")
    if h["status"] not in memory.LIVE_HYPOTHESIS_STATUSES:
        raise OwnerError(f"The hypothesis is {h['status']}; only live ones can be challenged.")
    critique_id = memory.add_critique(
        conn,
        target_id=hypothesis_id,
        argument=argument,
        alternative_explanation=(alternative or "").strip() or None,
        severity=severity,
    )
    jobs.enqueue(conn, "resolve", {"hypothesis_id": hypothesis_id, "round": 1}, priority=2)
    return critique_id


def set_mission(conn: Connection, statement: str, slug: str | None = None) -> UUID:
    """Set the organization's mission, or a domain's. The old one is kept,
    superseded, so the history of what the organization was for remains."""
    statement = statement.strip()
    if not statement:
        raise OwnerError("A mission needs a statement.")
    domain_id = domain(conn, slug)["id"] if slug else None
    current = memory.mission(conn, domain_id)
    if current is not None and current["statement"] == statement:
        raise OwnerError("That is already the mission.")
    return memory.set_mission(conn, statement, domain_id)


def ask(conn: Connection, text: str, slug: str | None = None) -> UUID:
    """A question for the organization, answered from memory at any hour."""
    text = text.strip()
    if not text:
        raise OwnerError("Ask a question.")
    domain_id = domain(conn, slug)["id"] if slug else None
    question_id = memory.add_question(conn, text, domain_id)
    jobs.enqueue(conn, "ask", {"question_id": question_id}, priority=1)
    return question_id


def add_domain(conn: Connection, slug: str, name: str, description: str | None = None) -> UUID:
    slug, name = slug.strip().lower(), name.strip()
    if not slug or not name:
        raise OwnerError("A domain needs a slug and a name.")
    try:
        with conn.transaction():
            return conn.execute(
                "INSERT INTO domains (slug, name, description) VALUES (%s, %s, %s) RETURNING id",
                (slug, name, (description or "").strip() or None),
            ).fetchone()["id"]
    except psycopg.errors.UniqueViolation as e:
        raise OwnerError(f"There is already a domain {slug!r}.") from e
    except psycopg.errors.CheckViolation as e:
        raise OwnerError(
            "A slug is lowercase letters and digits joined by hyphens, e.g. ai-agents."
        ) from e


def domain(conn: Connection, slug: str) -> dict:
    d = memory.domain_by_slug(conn, slug)
    if d is None:
        raise OwnerError(f"No domain {slug!r}.")
    return d


def set_domain_status(conn: Connection, slug: str, status: str) -> None:
    """Pausing a domain stops the scheduler planning it; retiring ends it."""
    if status not in DOMAIN_STATUSES:
        raise OwnerError(f"Unknown domain status {status!r}.")
    conn.execute("UPDATE domains SET status = %s WHERE id = %s", (status, domain(conn, slug)["id"]))


def set_discovery_sources(conn: Connection, slug: str, names: list[str], known: list[str]) -> None:
    """The Scout's discovery sources for a domain; empty means the default."""
    unknown = sorted(set(names) - set(known))
    if unknown:
        raise OwnerError(f"Unknown sources {unknown}; available: {known}.")
    conn.execute(
        "UPDATE domains SET discovery_sources = %s WHERE id = %s",
        (sorted(set(names)), domain(conn, slug)["id"]),
    )


def approve_feed(conn: Connection, slug: str, url: str, title: str | None, crawler) -> int:
    """Approve a feed for a domain after reading it once, so a mistyped or
    unreadable address is caught now. Returns how many items it has."""
    d = domain(conn, slug)
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise OwnerError("A feed address starts with http:// or https://.")
    try:
        items = crawler.crawl(url, max_items=100)
    except Exception as e:
        raise OwnerError(f"Could not read {url} as a feed: {e}") from e
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO approved_sources (domain_id, kind, url, title) "
                "VALUES (%s, 'feed', %s, %s)",
                (d["id"], url, (title or "").strip() or None),
            )
    except psycopg.errors.UniqueViolation as e:
        raise OwnerError(f"{url} is already approved for {slug}.") from e
    return len(items)


def set_feed_status(conn: Connection, slug: str, url: str, status: str) -> None:
    if status not in FEED_STATUSES:
        raise OwnerError(f"Unknown feed status {status!r}.")
    updated = conn.execute(
        "UPDATE approved_sources SET status = %s WHERE domain_id = %s AND url = %s",
        (status, domain(conn, slug)["id"], url),
    ).rowcount
    if not updated:
        raise OwnerError(f"No feed {url} for {slug}.")


def request_work(conn: Connection, kind: str, slug: str) -> UUID:
    """Ask for a scout or a planning run on a domain now (it still starts
    only within working hours, unless the worker is drained by hand)."""
    if kind not in REQUESTS:
        raise OwnerError(f"Unknown request {kind!r}.")
    d = domain(conn, slug)
    if d["status"] != "active":
        raise OwnerError(f"{slug} is {d['status']}; resume it first.")
    return jobs.enqueue(conn, kind, {"domain_id": d["id"]}, priority=2)
