"""Drafting a Moltbook post that asks other agents about a hypothesis.

The community agent's stage 2 (docs/decisions.md). The Researcher drafts
from memory only: the hypothesis, its evidence and the open critiques. The
draft is a proposed decision (topic 'post') for the owner, who approves,
edits or rejects it on the board; only then does the publisher, a separate
component with the key, send it. Code, not the model, decides the links
(only sources memory holds) and signs the post.
"""

from uuid import UUID

from pydantic import BaseModel, Field

from collegium import community, memory
from collegium.db import Connection
from collegium.jobs import Job
from collegium.reliability import NOT_WORTH_PAYING, classify
from collegium.roles.base import Context, Persist, Role
from collegium.untrusted import fence

# Sources listed under a post, supporting ones first.
MAX_SOURCES = 4
# Evidence shown to the model.
MAX_EVIDENCE = 8
# A model that runs on keeps the post within the forum's limit, signed.
MAX_BODY = 3000
MAX_QUESTION = 500


class PostDraft(BaseModel):
    title: str = Field(description="A specific question another agent could answer")
    body: str = Field(description="What the organization holds and why, 80 to 250 words, no links")
    question: str = Field(description="The one thing readers are asked for")
    submolt: str = Field(description="The forum, one of those listed")
    why: str = Field(description="Why outside feedback on this is worth asking for now")


class Drafter(Role):
    name = "researcher"
    job_kind = "draft"
    prompt_file = "draft.md"
    searches = False  # from memory only

    def prepare(self, ctx: Context, job: Job) -> Persist:
        hypothesis_id = UUID(job.payload["hypothesis_id"])
        with ctx.db.reading() as conn:
            h = memory.hypothesis(conn, hypothesis_id)
            if h is None:
                raise LookupError(f"hypothesis {hypothesis_id} not found")
            if memory.post_decision(conn, hypothesis_id, ("proposed",)):
                return lambda conn: "a draft about it is already waiting for the owner"
            evidence = memory.hypothesis_evidence(conn, hypothesis_id)
            critiques = memory.open_critiques(conn, hypothesis_id)
            domain_ids = memory.node_domain_ids(conn, hypothesis_id)

        evidence = [e for e in evidence if e["stance"] in ("supports", "contradicts")]
        draft = ctx.llm.generate(
            self.system_prompt(),
            _brief(h, evidence[:MAX_EVIDENCE], critiques) + "\n\nWhat is your draft?",
            PostDraft,
        )
        title = community.without_links(draft.title)[: community.MAX_TITLE]
        submolt = draft.submolt.strip().lower().removeprefix("m/")
        if submolt not in community.SUBMOLTS:
            submolt = community.DEFAULT_SUBMOLT
        content = community.compose(
            community.without_links(draft.body)[:MAX_BODY],
            community.without_links(draft.question)[:MAX_QUESTION],
            _sources(evidence),
        )

        def persist(conn: Connection) -> str:
            if memory.post_decision(conn, hypothesis_id, ("proposed",)):
                return "a draft about it is already waiting for the owner"
            decision_id = memory.add_decision(
                conn,
                statement=f"Post on Moltbook in m/{submolt}: {title}",
                rationale=draft.why.strip() or "Outside feedback on a hypothesis under study.",
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
            return f"drafted for m/{submolt}: {title}"

        return persist


def _sources(evidence: list[dict]) -> list[str]:
    """Addresses of the evidence the post rests on, supporting first. Only
    web pages that can carry evidence: no social media or forum posts."""
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


def _brief(h: dict, evidence: list[dict], critiques: list[dict]) -> str:
    conf = f"{h['confidence']:.2f}" if h["confidence"] is not None else "not yet assessed"
    lines = [
        f"Hypothesis ({h['status'].replace('_', ' ')}, confidence {conf}): {h['statement']}",
    ]
    if h.get("rationale"):
        lines.append(f"Why it was proposed: {h['rationale']}")
    lines.append("\nEvidence:")
    for i, e in enumerate(evidence, 1):
        lines.append(f"[X{i}] ({e['stance']}, reliability {e['reliability']}) {e['summary']}")
        if e["excerpt"]:
            lines.append(fence(f"X{i}", e["excerpt"]))
    if not evidence:
        lines.append("- none yet")
    lines.append("\nOpen critiques:")
    lines += [f"- (severity {k['severity']}) {k['argument']}" for k in critiques] or ["- none"]
    lines.append("\nForums you may post in:")
    lines += [f"- {name}: {about}" for name, about in community.SUBMOLTS.items()]
    return "\n".join(lines)
