"""Resolving critiques: the Researcher investigates what the Skeptic
objected to, and the Skeptic then settles each objection.

Queued by the Historian while a hypothesis has open critiques that block
its acceptance, for at most MAX_ROUNDS rounds, so a disputed hypothesis
cannot consume the budget without end.
"""

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
    critique_heading,
    flag_note,
    ground_evidence,
    render_documents,
    store_evidence,
)

MAX_ROUNDS = 2
# Searches per round: the critiques are specific, so a few targeted
# queries settle them better than many.
MAX_QUERIES = 2


class ResolutionFindings(BaseModel):
    evidence: list[EvidenceItem] = Field(max_length=6)


class Resolver(Role):
    name = "researcher"
    job_kind = "resolve"
    prompt_file = "resolver.md"

    def prepare(self, ctx: Context, job: Job) -> Persist:
        hypothesis_id = UUID(job.payload["hypothesis_id"])
        round_ = int(job.payload.get("round", 1))
        with ctx.db.reading() as conn:
            h = memory.hypothesis(conn, hypothesis_id)
            if h is None:
                raise LookupError(f"hypothesis {hypothesis_id} not found")
            critiques = memory.open_critiques(conn, hypothesis_id)
            domain_ids = memory.node_domain_ids(conn, hypothesis_id)
        if h["status"] not in memory.LIVE_HYPOTHESIS_STATUSES or not critiques:
            return lambda conn: (
                f"nothing to resolve: {h['status']}, {len(critiques)} open critiques"
            )

        labels = {f"C{i}": c["id"] for i, c in enumerate(critiques, 1)}
        brief = _brief(h, critiques)
        system = self.system_prompt()
        plan = ctx.llm.generate(
            system, brief + "\n\nWhich web searches would settle these critiques?", SearchPlan
        )
        queries = plan.queries[:MAX_QUERIES]
        documents = self.gather(ctx, queries)
        if not documents:
            raise NothingToWorkWith(f"no documents for {queries}")
        findings = ctx.llm.generate(
            system,
            brief
            + "\n\nDocuments:\n\n"
            + render_documents(documents)
            + "\n\nWhat evidence bears on the critiques and the hypothesis?",
            ResolutionFindings,
        )
        grounding = ground_evidence(findings.evidence, documents)

        def persist(conn: Connection) -> str:
            targets: dict[str, UUID | tuple[UUID, str]] = {"H": hypothesis_id}
            targets |= {label: (cid, "critique") for label, cid in labels.items()}
            outcome = store_evidence(conn, grounding.grounded, targets, domain_ids)
            jobs.enqueue(
                conn,
                "review",
                {"hypothesis_id": hypothesis_id, "round": round_},
                parent_job_id=job.id,
            )
            return (
                f"round {round_}: queries={queries}; {len(critiques)} open critiques; "
                f"{outcome.stored} evidence stored, {len(grounding.dropped)} ungrounded dropped; "
                f"sent back to the Skeptic.{grounding.describe_dropped()}" + flag_note(documents)
            )

        return persist


def _brief(h: dict, critiques: list[dict]) -> str:
    lines = [f"Hypothesis H: {h['statement']}", "\nOpen critiques:"]
    for i, c in enumerate(critiques, 1):
        alt = (
            f"\n    Alternative explanation: {c['alternative_explanation']}"
            if c["alternative_explanation"]
            else ""
        )
        lines.append(f"[C{i}] {critique_heading(c)} {c['argument']}{alt}")
    return "\n".join(lines)
