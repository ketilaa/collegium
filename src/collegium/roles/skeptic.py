"""The Skeptic: challenges a hypothesis, settles its earlier critiques and
adjusts confidence in it.

A first review (round 0) follows research. Later rounds follow the
Researcher's investigation of open critiques: the Skeptic then decides for
each critique whether it stands (upheld), has been answered (addressed) or
was mistaken (dismissed), and may raise at most one new critique, so the
loop converges.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from collegium import jobs, memory
from collegium.db import Connection
from collegium.jobs import Job
from collegium.roles.base import (
    Context,
    EvidenceItem,
    Persist,
    Role,
    SearchPlan,
    ground_evidence,
    render_documents,
    store_evidence,
)
from collegium.roles.base import (
    flag_note as _flag_note,
)
from collegium.untrusted import fence


class ProposedCritique(BaseModel):
    argument: str
    alternative_explanation: str | None = None
    severity: int = Field(ge=1, le=5)


class CritiqueResolution(BaseModel):
    critique: str = Field(description="Label of an open critique, e.g. C1")
    status: Literal["upheld", "addressed", "dismissed", "open"] = Field(
        description="upheld: the objection stands; addressed: the evidence answers it; "
        "dismissed: it was mistaken or irrelevant; open: not yet settled"
    )
    resolution: str = Field(description="Why, citing the evidence")


class SkepticReview(BaseModel):
    resolutions: list[CritiqueResolution] = Field(
        default_factory=list, description="One entry per open critique C1, C2, ..."
    )
    critiques: list[ProposedCritique] = Field(max_length=3, description="New critiques")
    evidence: list[EvidenceItem] = Field(max_length=5)
    confidence: float = Field(ge=0, le=1)
    confidence_rationale: str
    verdict: Literal["accept", "reject", "undecided"]


class Skeptic(Role):
    name = "skeptic"
    job_kind = "review"
    prompt_file = "skeptic.md"

    def prepare(self, ctx: Context, job: Job) -> Persist:
        hypothesis_id = UUID(job.payload["hypothesis_id"])
        round_ = int(job.payload.get("round", 0))
        with ctx.db.reading() as conn:
            h = memory.hypothesis(conn, hypothesis_id)
            if h is None:
                raise LookupError(f"hypothesis {hypothesis_id} not found")
            if h["status"] not in memory.LIVE_HYPOTHESIS_STATUSES:
                return lambda conn: f"skipped: hypothesis is {h['status']}"
            domain_ids = memory.node_domain_ids(conn, hypothesis_id)
            evidence = memory.hypothesis_evidence(conn, hypothesis_id)
            critiques = memory.critiques_of(conn, hypothesis_id)
            history = memory.confidence_history(conn, hypothesis_id)
            open_ = [c for c in critiques if c["status"] == "open"]
            about = {c["id"]: memory.citations(conn, c["id"]) for c in open_}

        labels = {f"C{i}": c["id"] for i, c in enumerate(open_, 1)}
        brief = _brief(h, evidence, critiques, open_, about, history)
        system = self.system_prompt()
        plan = ctx.llm.generate(
            system,
            brief + "\n\nWhich web searches would find counter-evidence or alternatives?",
            SearchPlan,
        )
        # The Skeptic can still reason about existing evidence when search
        # finds nothing new.
        documents = self.gather(ctx, plan.queries)
        review = ctx.llm.generate(
            system,
            brief
            + "\n\nNew documents:\n\n"
            + render_documents(documents)
            + "\n\nWhat is your review of hypothesis H?",
            SkepticReview,
        )
        grounding = ground_evidence(review.evidence, documents)
        # Settling critiques is the point of later rounds; new objections are
        # limited so the loop comes to an end.
        new_critiques = review.critiques if round_ == 0 else review.critiques[:1]

        def persist(conn: Connection) -> str:
            settled = 0
            for r in review.resolutions:
                critique_id = labels.get(r.critique.strip().upper())
                if critique_id and r.status != "open":
                    memory.set_critique_status(conn, critique_id, r.status, r.resolution)
                    settled += 1
            for c in new_critiques:
                memory.add_critique(
                    conn,
                    target_id=hypothesis_id,
                    argument=c.argument,
                    alternative_explanation=c.alternative_explanation,
                    severity=c.severity,
                )
            outcome = store_evidence(conn, grounding.grounded, {"H": hypothesis_id}, domain_ids)
            memory.assess_confidence(
                conn,
                target_id=hypothesis_id,
                target_kind="hypothesis",
                confidence=review.confidence,
                rationale=review.confidence_rationale,
            )
            jobs.enqueue(
                conn,
                "record",
                {"hypothesis_id": hypothesis_id, "verdict": review.verdict, "round": round_},
                parent_job_id=job.id,
            )
            return (
                f"round {round_}: queries={plan.queries}; verdict {review.verdict} at "
                f"{review.confidence:.2f}; {settled} of {len(open_)} open critiques settled, "
                f"{len(new_critiques)} new critiques, "
                f"{outcome.stored} evidence stored, {len(grounding.dropped)} ungrounded dropped."
                f"{grounding.describe_dropped()}" + _flag_note(documents)
            )

        return persist


def _brief(
    h: dict,
    evidence: list[dict],
    critiques: list[dict],
    open_: list[dict],
    about: dict,
    history: list[dict],
) -> str:
    lines = [f"Hypothesis H ({h['status']}): {h['statement']}"]
    if h.get("rationale"):
        lines.append(f"Rationale given: {h['rationale']}")
    lines.append("\nConfidence so far:")
    lines += [
        f"- {c['confidence']:.2f} by {c['assessed_by']}: {c['rationale']}" for c in history
    ] or ["- not assessed"]
    lines.append("\nEvidence on record:")
    # Excerpts are outside text, even when read back from memory.
    lines += [
        f"- ({e['stance']}) {e['summary']} [{e['source_uri']}]\n"
        + fence(f"EXCERPT{i}", e["excerpt"] or "")
        for i, e in enumerate(evidence, 1)
    ] or ["- none"]
    settled = [c for c in critiques if c["status"] != "open"]
    if settled:
        lines.append("\nSettled critiques:")
        lines += [
            f"- ({c['status']}, severity {c['severity']}) {c['argument']} Resolution: "
            f"{c['resolution']}"
            for c in settled
        ]
    lines.append("\nOpen critiques, to settle in your resolutions:")
    if not open_:
        lines.append("- none")
    for i, c in enumerate(open_, 1):
        alt = (
            f" Alternative: {c['alternative_explanation']}" if c["alternative_explanation"] else ""
        )
        lines.append(f"[C{i}] (severity {c['severity']}) {c['argument']}{alt}")
        for j, e in enumerate(about.get(c["id"], []), 1):
            lines.append(
                f"    Evidence {e['stance']} C{i}: {e['summary']} [{e['uri']}]\n"
                + fence(f"C{i}E{j}", e["excerpt"] or "")
            )
    return "\n".join(lines)
