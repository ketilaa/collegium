"""Corroboration: the Researcher looks for independent evidence on a
hypothesis, from sites not already cited, and sends it back for review.

Queued by the Strategist for hypotheses short of independent support.
"""

from uuid import UUID

from collegium import jobs, memory
from collegium.db import Connection
from collegium.jobs import Job
from collegium.roles.base import (
    Context,
    NothingToWorkWith,
    Persist,
    Role,
    SearchPlan,
    flag_note,
    ground_evidence,
    render_documents,
    store_evidence,
)
from collegium.roles.historian import site
from collegium.roles.resolver import ResolutionFindings


class Corroborator(Role):
    name = "researcher"
    job_kind = "corroborate"
    prompt_file = "corroborator.md"

    def prepare(self, ctx: Context, job: Job) -> Persist:
        hypothesis_id = UUID(job.payload["hypothesis_id"])
        with ctx.db.reading() as conn:
            h = memory.hypothesis(conn, hypothesis_id)
            if h is None:
                raise LookupError(f"hypothesis {hypothesis_id} not found")
            domain_ids = memory.node_domain_ids(conn, hypothesis_id)
            cited = {
                site(e["source_uri"])
                for e in memory.hypothesis_evidence(conn, hypothesis_id)
                if e["source_uri"]
            }
        if h["status"] not in memory.LIVE_HYPOTHESIS_STATUSES:
            return lambda conn: f"skipped: hypothesis is {h['status']}"

        brief = f"Hypothesis H: {h['statement']}"
        if h.get("rationale"):
            brief += f"\nRationale given: {h['rationale']}"
        if cited:
            brief += f"\nAlready cited (excluded): {', '.join(sorted(cited))}"
        system = self.system_prompt()
        plan = ctx.llm.generate(
            system, brief + "\n\nWhich web searches would find independent sources?", SearchPlan
        )
        documents = [d for d in self.gather(ctx, plan.queries) if site(d.url) not in cited]
        if not documents:
            raise NothingToWorkWith(f"no new independent documents for {plan.queries}")
        findings = ctx.llm.generate(
            system,
            brief
            + "\n\nDocuments:\n\n"
            + render_documents(documents)
            + "\n\nWhat evidence do these independent sources give on H?",
            ResolutionFindings,
        )
        grounding = ground_evidence(findings.evidence, documents)

        def persist(conn: Connection) -> str:
            outcome = store_evidence(conn, grounding.grounded, {"H": hypothesis_id}, domain_ids)
            jobs.enqueue(
                conn, "review", {"hypothesis_id": hypothesis_id, "round": 0}, parent_job_id=job.id
            )
            new_sites = {site(g.document.url) for g in grounding.grounded}
            return (
                f"queries={plan.queries}; {outcome.stored} evidence stored from "
                f"{len(new_sites)} new site(s), {len(grounding.dropped)} ungrounded dropped; "
                f"sent for review.{grounding.describe_dropped()}" + flag_note(documents)
            )

        return persist
