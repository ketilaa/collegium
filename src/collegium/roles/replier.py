"""Drafting a reply to another agent on Moltbook from what memory holds.

The community agent (docs/decisions.md). Three occasions:

- a Moltbook post or comment the Scout recorded as an observation: a reply
  is drafted only if memory has something to add;
- a comment in one of the organization's own threads: a reply is always
  drafted. If memory answers, it says so; if not, a fixed acknowledgement
  names what the organization will look into, a scout is sent to look,
  and the thread is marked for a follow-up;
- a follow-up to such an acknowledgement (queued by the Strategist while
  it is recent): drafted only once memory holds something on it.

Replies are held to the same rules as answers to the owner
(`answerer.compose`): only records shown count, and a point may claim no
more firmness than memory gives it. The only text not grounded in memory
is the acknowledgement, which is fixed by code apart from naming the
subject. Every draft is a proposed decision (topic 'reply'); nothing is
sent unless the owner approves it, and then only by the publisher.
"""

import re
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, Field

from collegium import community, jobs, memory
from collegium.db import Connection
from collegium.jobs import Job
from collegium.reliability import NOT_WORTH_PAYING, classify
from collegium.roles.answerer import Answer, AnswerPoint, SearchTerms, brief, compose
from collegium.roles.base import Context, Persist, Role
from collegium.untrusted import fence

MAX_SOURCES = 4
MAX_SUBJECT_CHARS = 150
_NUMBERS = re.compile(r"\s*(?:\[\d+\])+")


class ReplyDraft(BaseModel):
    worth_replying: bool = Field(description="Whether memory holds something worth saying")
    points: list[AnswerPoint] = Field(default_factory=list, max_length=4)
    look_into: str | None = Field(
        None, description="In our own thread, when memory cannot answer: what we will look into"
    )
    why: str = Field("", description="Why this reply is worth sending")


@dataclass
class Thread:
    url: str  # the post or comment answered, as recorded
    post_id: str
    comment_id: str | None
    author: str
    text: str  # what the other agent wrote (outside text)
    statement: str | None  # what the organization recorded from it, if anything
    own_post: dict | None  # our post, when this is our thread
    anchor: UUID | None  # the observation it concerns, if any
    domain_ids: list[UUID]


class Replier(Role):
    name = "researcher"
    job_kind = "reply"
    prompt_file = "reply.md"
    searches = False  # from memory only; the thread was read by the Scout

    def prepare(self, ctx: Context, job: Job) -> Persist:
        if "follows" in job.payload:
            return self._follow_up(ctx, UUID(job.payload["follows"]))
        with ctx.db.reading() as conn:
            thread = _thread(conn, job.payload)
            if isinstance(thread, str):
                return lambda conn: thread
            if memory.reply_decision(conn, thread.url):
                return lambda conn: "already drafted"

        own = thread.own_post is not None
        about = _about(thread)
        system = self.system_prompt()
        plan = ctx.llm.generate(
            system,
            about + "\n\nWhich search terms would find what memory holds on it?",
            SearchTerms,
        )
        with ctx.db.reading() as conn:
            found = memory.recall(
                conn, plan.terms, thread.domain_ids[0] if thread.domain_ids else None
            )
            if thread.anchor:  # the thread itself is not something to cite back at it
                found["observations"] = [
                    r for r in found["observations"] if r["id"] != thread.anchor
                ]
                found["evidence"] = [
                    r for r in found["evidence"] if r["target_id"] != thread.anchor
                ]
        labels, records, shown = brief(found)
        if not labels and not own:
            return lambda conn: f"terms={plan.terms}; memory holds nothing on it"

        ask = (
            "This is the organization's own thread: a reply is expected. If memory does not "
            "answer, give no points and say in look_into what the organization will look into.\n\n"
            if own
            else ""
        )
        draft = ctx.llm.generate(
            system,
            f"{about}\n\nWhat memory holds:\n\n{shown or '(nothing)'}\n\n{ask}"
            "Is a reply worth sending, and what is it?",
            ReplyDraft,
        )
        composed = compose(Answer(points=draft.points), labels, records)
        look_into = _subject(draft.look_into) if own else None
        if composed.cited and (draft.worth_replying or own):
            content = _grounded(ctx, composed)
            look_into = None
        elif own and look_into:
            content = community.acknowledgement(look_into)
        else:
            return lambda conn: f"terms={plan.terms}; nothing worth replying"
        if community.reply_problems(content):
            return lambda conn: "draft too long; not proposed"

        def persist(conn: Connection) -> str:
            if memory.reply_decision(conn, thread.url):
                return "already drafted"
            decision_id = _propose(conn, thread, content, draft.why, composed.cited, look_into)
            if look_into and thread.domain_ids:
                # Look for it now, so there is something to follow up with.
                jobs.enqueue(
                    conn,
                    "scout",
                    {"domain_id": thread.domain_ids[0], "focus": look_into},
                    parent_job_id=job.id,
                )
            what = (
                f"an acknowledgement; looking into: {look_into}"
                if look_into
                else f"a reply citing {len(composed.cited)} records"
            )
            return f"terms={plan.terms}; drafted {what} (decision {str(decision_id)[:8]})"

        return persist

    def _follow_up(self, ctx: Context, acknowledged: UUID) -> Persist:
        """A follow-up in a thread where the organization said it would
        look into something: drafted only once memory holds something."""
        with ctx.db.reading() as conn:
            earlier = conn.execute(
                "SELECT * FROM decisions WHERE id = %s AND topic = 'reply'", (acknowledged,)
            ).fetchone()
            if earlier is None or not (earlier["details"] or {}).get("look_into"):
                return lambda conn: "nothing to follow up"
            if memory.follow_up_decision(conn, acknowledged):
                return lambda conn: "already followed up"
            domain_ids = memory.node_domain_ids(conn, acknowledged)
        d = earlier["details"]
        subject = d["look_into"]
        about = (
            f"In the organization's own thread on Moltbook, agent {d.get('author')} asked:\n"
            f"{fence('T', d.get('comment') or '')}\n\n"
            f"The organization replied that it would look into: {subject}"
        )
        system = self.system_prompt()
        plan = ctx.llm.generate(
            system, about + "\n\nWhich search terms would find what memory now holds?", SearchTerms
        )
        with ctx.db.reading() as conn:
            found = memory.recall(conn, plan.terms, domain_ids[0] if domain_ids else None)
        labels, records, shown = brief(found)
        if not labels:
            return lambda conn: f"terms={plan.terms}; nothing yet on {subject}"
        draft = ctx.llm.generate(
            system,
            f"{about}\n\nWhat memory now holds:\n\n{shown}\n\nThis is a follow-up: reply only "
            "if memory now holds something on what was asked. Is a follow-up worth sending?",
            ReplyDraft,
        )
        composed = compose(Answer(points=draft.points), labels, records)
        if not draft.worth_replying or not composed.cited:
            return lambda conn: f"terms={plan.terms}; nothing worth following up yet"
        content = _grounded(ctx, composed)
        if community.reply_problems(content):
            return lambda conn: "draft too long; not proposed"
        thread = Thread(
            url=d["thread_url"],
            post_id=d["reply_to_post"],
            comment_id=d.get("reply_to_comment"),
            author=d.get("author") or "another agent",
            text=d.get("comment") or "",
            statement=None,
            own_post=None,
            anchor=None,
            domain_ids=domain_ids,
        )

        def persist(conn: Connection) -> str:
            if memory.follow_up_decision(conn, acknowledged):
                return "already followed up"
            decision_id = _propose(
                conn, thread, content, draft.why, composed.cited, None, follows=acknowledged
            )
            return (
                f"terms={plan.terms}; drafted a follow-up citing {len(composed.cited)} records "
                f"(decision {str(decision_id)[:8]})"
            )

        return persist


def _thread(conn: Connection, payload: dict) -> Thread | str:
    """The post or comment to answer, from a recorded observation or, in
    the organization's own thread, from the comment's source."""
    if "observation_id" in payload:
        o = memory.observation(conn, UUID(payload["observation_id"]))
        if o is None:
            raise LookupError(f"observation {payload['observation_id']} not found")
        url, title, metadata, statement = (
            o["source_uri"] or "",
            o["source_title"],
            o["source_metadata"] or {},
            o["statement"],
        )
        anchor = o["id"]
        domain_ids = memory.node_domain_ids(conn, anchor)
    else:
        s = memory.source(conn, UUID(payload["source_id"]))
        if s is None:
            raise LookupError(f"source {payload['source_id']} not found")
        url, title, metadata, statement, anchor = (
            s["uri"] or "",
            s["title"],
            s["metadata"],
            None,
            None,
        )
        domain_ids = []
    where = community.thread(url)
    if where is None:
        return "not a Moltbook post or comment"
    own = memory.own_post(conn, where[0])
    if own is not None and own["domain_id"] and not domain_ids:
        domain_ids = [own["domain_id"]]
    return Thread(
        url=url,
        post_id=where[0],
        comment_id=where[1],
        author=metadata.get("moltbook_author") or "another agent",
        text=f"{title or ''}\n{metadata.get('snippet') or ''}",
        statement=statement,
        own_post=own,
        anchor=anchor,
        domain_ids=domain_ids,
    )


def _about(thread: Thread) -> str:
    """The thread as the model sees it: our own post (our words), then what
    the other agent wrote (outside text, fenced)."""
    parts = []
    if thread.own_post:
        parts.append(
            f"The organization's own post on Moltbook, titled {thread.own_post['title']!r}:\n"
            f"{(thread.own_post['content'] or '')[:1500]}"
        )
    kind = "comment" if thread.comment_id else "post"
    parts.append(f"A Moltbook {kind} by agent {thread.author}:\n{fence('T', thread.text)}")
    if thread.statement:
        parts.append(f"The organization recorded from it: {thread.statement}")
    return "\n\n".join(parts)


def _subject(text: str | None) -> str | None:
    """What we will look into, as the acknowledgement names it."""
    if not text:
        return None
    first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0]  # one phrase
    subject = community.without_links(" ".join(first.split()))[:MAX_SUBJECT_CHARS]
    return subject.rstrip(" .") or None


def _grounded(ctx: Context, composed) -> str:
    with ctx.db.reading() as conn:
        sources = [
            u
            for u in memory.source_uris_of(conn, composed.cited)
            if u.startswith(("https://", "http://")) and classify(u)[0] not in NOT_WORTH_PAYING
        ][:MAX_SOURCES]
    points = [community.without_links(_NUMBERS.sub("", p["text"])) for p in composed.points]
    return community.compose_reply(points, sources)


def _propose(
    conn: Connection,
    thread: Thread,
    content: str,
    why: str,
    cited: list[UUID],
    look_into: str | None,
    follows: UUID | None = None,
) -> UUID:
    subject = thread.statement or (thread.own_post or {}).get("title") or "our thread"
    statement = (
        f"Follow up on Moltbook with agent {thread.author}: {subject}"
        if follows
        else f"Reply on Moltbook to agent {thread.author}: {subject}"
    )
    details = {
        "forum": community.FORUM,
        "domain_id": str(thread.domain_ids[0]) if thread.domain_ids else None,
        "observation_id": str(thread.anchor) if thread.anchor else None,
        "reply_to_post": thread.post_id,
        "reply_to_comment": thread.comment_id,
        "thread_url": thread.url,
        "author": thread.author,
        "comment": thread.text[:1500],
        "content": content,
    }
    if look_into:
        details["look_into"] = look_into
    if follows:
        details["follows"] = str(follows)
    decision_id = memory.add_decision(
        conn,
        statement=statement,
        rationale=why.strip()
        or ("Memory now holds something on it." if follows else "A reply in our thread."),
        topic="reply",
        details=details,
    )
    memory.tag_domains(conn, decision_id, thread.domain_ids)
    if thread.anchor:
        memory.add_relationship(
            conn, subject_id=decision_id, predicate="concerns", object_id=thread.anchor
        )
    if follows:
        memory.add_relationship(
            conn, subject_id=decision_id, predicate="follows", object_id=follows
        )
    for node_id in cited:
        memory.add_relationship(conn, subject_id=decision_id, predicate="cites", object_id=node_id)
    return decision_id
