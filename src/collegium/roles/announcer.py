"""Drafting a Moltbook post announcing that the organization changed its
mind: a hypothesis it once held was retired (a serious objection was never
settled) or superseded (a later hypothesis refined it, replacing it).

Queued by the Historian, deterministically, at the exact moment the status
change happens (`historian.py`) -- not by the Strategist's judgment, the
same way the "one scout a day" floor is not. A retirement is always
announced; a supersession only when the hypothesis being replaced was
itself `accepted` (a settled belief being revised, not a still-provisional
one being refined, which is just ordinary research progress). The
Researcher drafts from memory only, the same discipline as every other
community draft (`drafter.py`): what the organization believed, what came
in that changed it, and what it believes now, if anything. The draft is a
proposed decision (topic 'post') for the owner, exactly like any other
Moltbook post; nothing here bypasses approval or the publisher.
"""

from uuid import UUID

from pydantic import BaseModel, Field

from collegium import community, memory
from collegium.db import Connection
from collegium.jobs import Job
from collegium.reliability import NOT_WORTH_PAYING, classify
from collegium.roles.base import Context, Persist, Role
from collegium.roles.historian import blocking
from collegium.untrusted import fence

MAX_SOURCES = 4
MAX_EVIDENCE = 8
MAX_BODY = 3000
MAX_QUESTION = 500


class AnnouncementDraft(BaseModel):
    title: str = Field(description="A specific, plain statement of what changed")
    body: str = Field(
        description="What we believed, what changed, what we believe now (if "
        "anything); 80 to 250 words, no links"
    )
    question: str = Field(description="What outside perspective, if any, would help now")
    submolt: str = Field(description="The forum, one of those listed")
    why: str = Field(description="Why this is worth announcing, for the owner")


class Announcer(Role):
    name = "researcher"
    job_kind = "announce"
    prompt_file = "announce.md"
    searches = False  # from memory only

    def prepare(self, ctx: Context, job: Job) -> Persist:
        hypothesis_id = UUID(job.payload["hypothesis_id"])
        reason = job.payload["reason"]  # "retired" | "superseded"
        superseded_by = job.payload.get("superseded_by")
        with ctx.db.reading() as conn:
            h = memory.hypothesis(conn, hypothesis_id)
            if h is None:
                raise LookupError(f"hypothesis {hypothesis_id} not found")
            if memory.post_decision(conn, hypothesis_id, ("proposed",)):
                return lambda conn: "a draft about it is already waiting for the owner"
            evidence = memory.hypothesis_evidence(conn, hypothesis_id)
            critiques = memory.critiques_of(conn, hypothesis_id)
            history = memory.confidence_history(conn, hypothesis_id)
            domain_ids = memory.node_domain_ids(conn, hypothesis_id)
            new_h = memory.hypothesis(conn, UUID(superseded_by)) if superseded_by else None
            new_evidence = (
                memory.hypothesis_evidence(conn, UUID(superseded_by)) if superseded_by else []
            )

        evidence = [e for e in evidence if e["stance"] in ("supports", "contradicts")]
        new_evidence = [e for e in new_evidence if e["stance"] in ("supports", "contradicts")]
        draft = ctx.llm.generate(
            self.system_prompt(),
            _brief(reason, h, evidence[:MAX_EVIDENCE], critiques, history, new_h, new_evidence)
            + "\n\nWhat is your announcement?",
            AnnouncementDraft,
        )
        title = community.without_links(draft.title)[: community.MAX_TITLE]
        submolt = draft.submolt.strip().lower().removeprefix("m/")
        if submolt not in community.SUBMOLTS:
            submolt = community.DEFAULT_SUBMOLT
        sources = _sources(evidence + new_evidence)
        content = community.compose(
            community.without_links(draft.body)[:MAX_BODY],
            community.without_links(draft.question)[:MAX_QUESTION],
            sources,
        )

        def persist(conn: Connection) -> str:
            if memory.post_decision(conn, hypothesis_id, ("proposed",)):
                return "a draft about it is already waiting for the owner"
            decision_id = memory.add_decision(
                conn,
                statement=f"Post on Moltbook in m/{submolt}: {title}",
                rationale=draft.why.strip() or f"Announcing that a hypothesis was {reason}.",
                topic="post",
                details={
                    "forum": community.FORUM,
                    "domain_id": str(domain_ids[0]) if domain_ids else None,
                    "hypothesis_id": str(hypothesis_id),
                    "submolt": submolt,
                    "title": title,
                    "content": content,
                },
            )
            memory.tag_domains(conn, decision_id, domain_ids)
            memory.add_relationship(
                conn, subject_id=decision_id, predicate="concerns", object_id=hypothesis_id
            )
            return f"drafted ({reason}) for m/{submolt}: {title}"

        return persist


def _sources(evidence: list[dict]) -> list[str]:
    """Addresses of the evidence the announcement rests on. Only web pages
    that can carry evidence: no social media or forum posts."""
    ordered = sorted(evidence, key=lambda e: e["stance"] != "supports")
    urls: list[str] = []
    for e in ordered:
        uri = e["source_uri"] or ""
        if not uri.startswith(("https://", "http://")) or uri in urls:
            continue
        if classify(uri)[0] in NOT_WORTH_PAYING:
            continue
        urls.append(uri)
    return urls[:MAX_SOURCES]


def _brief(
    reason: str,
    h: dict,
    evidence: list[dict],
    critiques: list[dict],
    history: list[dict],
    new_h: dict | None,
    new_evidence: list[dict],
) -> str:
    conf = f"{h['confidence']:.2f}" if h["confidence"] is not None else "not yet assessed"
    lines = [
        f"What we believed ({reason}, last confidence {conf}): {h['statement']}",
    ]
    if h.get("rationale"):
        lines.append(f"Why it was proposed: {h['rationale']}")
    lines.append("\nHow confidence in it changed over time:")
    lines += [f"- {c['confidence']:.2f} by {c['assessed_by']}: {c['rationale']}" for c in history]
    lines.append("\nEvidence it rested on:")
    for i, e in enumerate(evidence, 1):
        lines.append(f"[X{i}] ({e['stance']}, reliability {e['reliability']}) {e['summary']}")
        if e["excerpt"]:
            lines.append(fence(f"X{i}", e["excerpt"]))
    if not evidence:
        lines.append("- none")
    if reason == "retired":
        lines.append("\nThe serious objection(s) that were never settled:")
        lines += [f"- {c['argument']}" for c in blocking(critiques)] or ["- none recorded"]
    elif new_h is not None:
        conf2 = f"{new_h['confidence']:.2f}" if new_h["confidence"] is not None else "not assessed"
        lines.append(f"\nWhat we believe now instead (confidence {conf2}): {new_h['statement']}")
        lines.append("New evidence behind it:")
        for i, e in enumerate(new_evidence, 1):
            lines.append(f"[Y{i}] ({e['stance']}, reliability {e['reliability']}) {e['summary']}")
            if e["excerpt"]:
                lines.append(fence(f"Y{i}", e["excerpt"]))
        if not new_evidence:
            lines.append("- none")
    lines.append("\nForums you may post in:")
    lines += [f"- {name}: {about}" for name, about in community.SUBMOLTS.items()]
    return "\n".join(lines)
