"""The Researcher: turns an observation into hypotheses and evidence."""

from uuid import UUID

from pydantic import BaseModel, Field

from collegium import jobs, memory
from collegium.db import Connection
from collegium.jobs import Job
from collegium.roles.base import (
    Context,
    EvidenceItem,
    NothingToWorkWith,
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


class ProposedHypothesis(BaseModel):
    label: str = Field(description="New label: H1, H2, ...")
    statement: str = Field(description="A general, testable claim")
    rationale: str
    refines: str | None = Field(
        None, description="Label (E1, E2, ...) of an existing hypothesis this one refines, if any"
    )
    confidence: float = Field(ge=0, le=1)


class ResearchFindings(BaseModel):
    hypotheses: list[ProposedHypothesis] = Field(max_length=3)
    evidence: list[EvidenceItem] = Field(max_length=8)


class Researcher(Role):
    name = "researcher"
    job_kind = "research"
    prompt_file = "researcher.md"

    def prepare(self, ctx: Context, job: Job) -> Persist:
        observation_id = UUID(job.payload["observation_id"])
        with ctx.db.reading() as conn:
            obs = memory.observation(conn, observation_id)
            if obs is None:
                raise LookupError(f"observation {observation_id} not found")
            domain_ids = memory.node_domain_ids(conn, observation_id)
            existing = memory.live_hypotheses(conn, domain_ids)

        existing_labels = {f"E{i}": h["id"] for i, h in enumerate(existing, 1)}
        brief = _brief(obs, existing)
        system = self.system_prompt()
        plan = ctx.llm.generate(
            system, brief + "\n\nWhich web searches would find evidence about this?", SearchPlan
        )
        documents = self.gather(ctx, plan.queries)
        if not documents:
            raise NothingToWorkWith(f"no documents for {plan.queries}")

        findings = ctx.llm.generate(
            system,
            brief
            + "\n\nDocuments:\n\n"
            + render_documents(documents)
            + "\n\nWhat hypotheses and evidence follow from this?",
            ResearchFindings,
        )
        grounding = ground_evidence(findings.evidence, documents)
        # A new hypothesis is only worth remembering if some evidence that is
        # actually in the sources supports it.
        supported = grounding.supported()

        def persist(conn: Connection) -> str:
            targets = dict(existing_labels)
            new_ids: list[UUID] = []
            matched: list[UUID] = []
            unsupported = 0
            for h in findings.hypotheses:
                label = h.label.strip().upper()
                if label in targets:  # a label that collides with E1.. or a repeat
                    continue
                if label not in supported:
                    unsupported += 1
                    continue
                # The model sometimes re-proposes a hypothesis it was shown, and
                # another job may have created it since this one read memory.
                # The evidence then belongs to the hypothesis already held.
                existing_id = memory.find_live_hypothesis(conn, h.statement, domain_ids)
                if existing_id:
                    if not memory.has_relationship(
                        conn, existing_id, "derived_from", observation_id
                    ):
                        memory.add_relationship(
                            conn,
                            subject_id=existing_id,
                            predicate="derived_from",
                            object_id=observation_id,
                        )
                    targets[label] = existing_id
                    matched.append(existing_id)
                    continue
                hypothesis_id = memory.add_hypothesis(
                    conn, statement=h.statement, rationale=h.rationale
                )
                memory.tag_domains(conn, hypothesis_id, domain_ids)
                memory.add_relationship(
                    conn,
                    subject_id=hypothesis_id,
                    predicate="derived_from",
                    object_id=observation_id,
                )
                refined = existing_labels.get((h.refines or "").strip().upper())
                if refined:
                    memory.add_relationship(
                        conn, subject_id=hypothesis_id, predicate="refines", object_id=refined
                    )
                memory.assess_confidence(
                    conn,
                    target_id=hypothesis_id,
                    target_kind="hypothesis",
                    confidence=h.confidence,
                    rationale="Initial assessment when proposed.",
                )
                targets[label] = hypothesis_id
                new_ids.append(hypothesis_id)

            outcome = store_evidence(conn, grounding.grounded, targets, domain_ids)
            memory.set_observation_status(
                conn, observation_id, "accepted" if new_ids or outcome.stored else "dismissed"
            )
            to_review = list(dict.fromkeys(new_ids + matched + sorted(outcome.touched, key=str)))
            for hypothesis_id in to_review:
                jobs.enqueue(conn, "review", {"hypothesis_id": hypothesis_id}, parent_job_id=job.id)
            if outcome.stored:
                jobs.enqueue(
                    conn,
                    "map",
                    {"observation_id": observation_id, "evidence_ids": outcome.evidence_ids},
                    parent_job_id=job.id,
                    priority=4,  # after the review work it runs beside
                )
            return (
                f"queries={plan.queries}; {len(new_ids)} new hypotheses "
                f"({unsupported} without grounded support dropped, "
                f"{len(matched)} matched existing), "
                f"{outcome.stored} evidence stored, {len(grounding.dropped)} ungrounded "
                f"excerpts dropped, {outcome.unlinked} unlinked; "
                f"{len(to_review)} sent for review.{grounding.describe_dropped()}"
                + _flag_note(documents)
            )

        return persist


def _brief(obs: dict, existing: list[dict]) -> str:
    lines = [f"Observation: {obs['statement']}"]
    if obs.get("recommendation"):
        lines.append(f"Why it may matter: {obs['recommendation']}")
    if obs.get("source_uri"):
        lines.append(f"Reported by: {obs['source_title'] or ''} {obs['source_uri']}")
    lines.append("\nExisting hypotheses in this domain:")
    if existing:
        for i, h in enumerate(existing, 1):
            conf = f"{h['confidence']:.2f}" if h["confidence"] is not None else "unassessed"
            lines.append(f"[E{i}] ({h['status']}, confidence {conf}) {h['statement']}")
    else:
        lines.append("- none yet")
    return "\n".join(lines)
