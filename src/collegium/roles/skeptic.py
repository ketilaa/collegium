"""The Skeptic: challenges a hypothesis and adjusts confidence in it."""

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


class ProposedCritique(BaseModel):
    argument: str
    alternative_explanation: str | None = None
    severity: int = Field(ge=1, le=5)


class SkepticReview(BaseModel):
    critiques: list[ProposedCritique] = Field(max_length=3)
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

        brief = _brief(h, evidence, critiques, history)
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

        def persist(conn: Connection) -> str:
            for c in review.critiques:
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
                {"hypothesis_id": hypothesis_id, "verdict": review.verdict},
                parent_job_id=job.id,
            )
            return (
                f"queries={plan.queries}; verdict {review.verdict} at "
                f"{review.confidence:.2f}; {len(review.critiques)} critiques, "
                f"{outcome.stored} evidence stored, {len(grounding.dropped)} ungrounded dropped."
                f"{grounding.describe_dropped()}"
            )

        return persist


def _brief(h: dict, evidence: list[dict], critiques: list[dict], history: list[dict]) -> str:
    lines = [f"Hypothesis H ({h['status']}): {h['statement']}"]
    if h.get("rationale"):
        lines.append(f"Rationale given: {h['rationale']}")
    lines.append("\nConfidence so far:")
    lines += [
        f"- {c['confidence']:.2f} by {c['assessed_by']}: {c['rationale']}" for c in history
    ] or ["- not assessed"]
    lines.append("\nEvidence on record:")
    lines += [
        f'- ({e["stance"]}) {e["summary"]} — "{e["excerpt"]}" [{e["source_uri"]}]' for e in evidence
    ] or ["- none"]
    lines.append("\nEarlier critiques:")
    lines += [
        f"- ({c['status']}, severity {c['severity']}) {c['argument']}" for c in critiques
    ] or ["- none"]
    return "\n".join(lines)
