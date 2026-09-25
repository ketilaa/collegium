"""Drafting a reply to another agent on Moltbook from what memory holds.

The community agent's stage 2 (docs/decisions.md). The Researcher reads a
Moltbook post or comment the Scout recorded, recalls what memory holds on
it, and drafts a reply only if memory has something to add. The reply is
held to the same rules as an answer to the owner (`answerer.compose`):
only records it was shown count, and a point may claim no more firmness
than memory gives it. The draft is a proposed decision (topic 'reply');
nothing is sent unless the owner approves it, and then only by the
publisher.
"""

import re
from uuid import UUID

from pydantic import BaseModel, Field

from collegium import community, memory
from collegium.db import Connection
from collegium.jobs import Job
from collegium.reliability import NOT_WORTH_PAYING, classify
from collegium.roles.answerer import Answer, AnswerPoint, SearchTerms, brief, compose
from collegium.roles.base import Context, Persist, Role
from collegium.untrusted import fence

MAX_SOURCES = 4
_NUMBERS = re.compile(r"\s*(?:\[\d+\])+")


class ReplyDraft(BaseModel):
    worth_replying: bool = Field(description="Whether memory holds something worth saying")
    points: list[AnswerPoint] = Field(default_factory=list, max_length=4)
    why: str = Field("", description="Why this reply is worth sending")


class Replier(Role):
    name = "researcher"
    job_kind = "reply"
    prompt_file = "reply.md"
    searches = False  # from memory only; the thread was read by the Scout

    def prepare(self, ctx: Context, job: Job) -> Persist:
        observation_id = UUID(job.payload["observation_id"])
        with ctx.db.reading() as conn:
            o = memory.observation(conn, observation_id)
            if o is None:
                raise LookupError(f"observation {observation_id} not found")
            where = community.thread(o["source_uri"] or "")
            if where is None:
                return lambda conn: "not a Moltbook post or comment"
            if memory.post_decision(conn, observation_id, ("proposed", "approved"), "reply"):
                return lambda conn: "already drafted"
            domain_ids = memory.node_domain_ids(conn, observation_id)

        metadata = o["source_metadata"] or {}
        author = metadata.get("moltbook_author") or "another agent"
        thread_text = fence("T", f"{o['source_title'] or ''}\n{metadata.get('snippet') or ''}")
        about = (
            f"A Moltbook {'comment' if where[1] else 'post'} by agent {author}:\n{thread_text}\n\n"
            f"The organization recorded from it: {o['statement']}"
        )
        system = self.system_prompt()
        plan = ctx.llm.generate(
            system,
            about + "\n\nWhich search terms would find what memory holds on it?",
            SearchTerms,
        )
        with ctx.db.reading() as conn:
            found = memory.recall(conn, plan.terms, domain_ids[0] if domain_ids else None)
            # The thread itself is not something to cite back at its author.
            found["observations"] = [r for r in found["observations"] if r["id"] != observation_id]
            found["evidence"] = [r for r in found["evidence"] if r["target_id"] != observation_id]
        labels, records, shown = brief(found)
        if not labels:
            return lambda conn: f"terms={plan.terms}; memory holds nothing on it"

        draft = ctx.llm.generate(
            system,
            f"{about}\n\nWhat memory holds:\n\n{shown}\n\n"
            "Is a reply worth sending, and what is it?",
            ReplyDraft,
        )
        composed = compose(Answer(points=draft.points), labels, records)
        if not draft.worth_replying or not composed.cited:
            return lambda conn: f"terms={plan.terms}; nothing worth replying"
        with ctx.db.reading() as conn:
            sources = [
                u
                for u in memory.source_uris_of(conn, composed.cited)
                if u.startswith(("https://", "http://")) and classify(u)[0] not in NOT_WORTH_PAYING
            ][:MAX_SOURCES]
        points = [community.without_links(_NUMBERS.sub("", p["text"])) for p in composed.points]
        content = community.compose_reply(points, sources)
        if community.reply_problems(content):
            return lambda conn: "draft too long; not proposed"
        post_id, comment_id = where

        def persist(conn: Connection) -> str:
            if memory.post_decision(conn, observation_id, ("proposed", "approved"), "reply"):
                return "already drafted"
            decision_id = memory.add_decision(
                conn,
                statement=f"Reply on Moltbook to agent {author}: {o['statement']}",
                rationale=draft.why.strip() or "Memory holds something that bears on it.",
                topic="reply",
                details={
                    "forum": community.FORUM,
                    "domain_id": str(domain_ids[0]) if domain_ids else None,
                    "observation_id": str(observation_id),
                    "reply_to_post": post_id,
                    "reply_to_comment": comment_id,
                    "thread_url": o["source_uri"],
                    "author": author,
                    "content": content,
                },
            )
            memory.tag_domains(conn, decision_id, domain_ids)
            memory.add_relationship(
                conn, subject_id=decision_id, predicate="concerns", object_id=observation_id
            )
            for node_id in composed.cited:
                memory.add_relationship(
                    conn, subject_id=decision_id, predicate="cites", object_id=node_id
                )
            return f"terms={plan.terms}; drafted a reply citing {len(composed.cited)} records"

        return persist
