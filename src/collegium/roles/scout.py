"""The Scout: breadth-first exploration of a domain, producing observations."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, Field

from collegium import jobs, memory
from collegium.acquisition import SearchResult
from collegium.dates import parse_date
from collegium.db import Connection
from collegium.jobs import Job
from collegium.roles.base import Context, NothingToWorkWith, Persist, Role, SearchPlan


class ProposedObservation(BaseModel):
    statement: str = Field(description="One factual sentence about what was observed")
    source: int = Field(description="Number of the search result it comes from, e.g. 3 for [R3]")
    occurred_at: str | None = Field(
        None, description="ISO date the event happened, if the result says"
    )
    why_it_matters: str
    investigate: bool


class ScoutReport(BaseModel):
    observations: list[ProposedObservation] = Field(max_length=5)


class Scout(Role):
    name = "scout"
    job_kind = "scout"
    prompt_file = "scout.md"

    def prepare(self, ctx: Context, job: Job) -> Persist:
        domain_id = UUID(job.payload["domain_id"])
        with ctx.db.reading() as conn:
            domain = memory.domain(conn, domain_id)
            recent = memory.recent_observations(conn, domain_id)
        if domain is None:
            raise LookupError(f"domain {domain_id} not found")

        brief = _brief(domain, recent)
        system = self.system_prompt()
        plan = ctx.llm.generate(
            system, brief + "\n\nWhich web searches should you run now?", SearchPlan
        )
        if ctx.acquisition is None:
            raise NothingToWorkWith("no acquisition provider configured")
        results, queries = _search(ctx, plan.queries, domain["discovery_sources"])
        if not results:
            raise NothingToWorkWith(f"no search results for {plan.queries}")

        report = ctx.llm.generate(
            system,
            brief
            + "\n\nSearch results:\n\n"
            + _render_results(results)
            + "\n\nWhich observations should the organization record?",
            ScoutReport,
        )
        known = {_key(o["statement"]) for o in recent}

        def persist(conn: Connection) -> str:
            recorded = investigating = skipped = 0
            for obs in report.observations:
                if not 1 <= obs.source <= len(results) or _key(obs.statement) in known:
                    skipped += 1
                    continue
                known.add(_key(obs.statement))
                result = results[obs.source - 1]
                source_id = memory.record_source(
                    conn,
                    uri=result.url,
                    title=result.title,
                    published_at=result.published_at,
                    metadata={
                        "provider": result.provider,
                        "query": queries[result.url],
                        "snippet": result.snippet,
                        **result.metadata,
                    },
                    acquisition_id=result.acquisition_id,
                )
                observation_id = memory.add_observation(
                    conn,
                    statement=obs.statement,
                    source_id=source_id,
                    recommendation=obs.why_it_matters,
                    occurred_at=obs.occurred_at or result.published_at,
                    status="investigating" if obs.investigate else "proposed",
                )
                memory.tag_domains(conn, observation_id, [domain_id])
                recorded += 1
                if obs.investigate:
                    jobs.enqueue(
                        conn, "research", {"observation_id": observation_id}, parent_job_id=job.id
                    )
                    investigating += 1
            return (
                f"queries={plan.queries}; recorded {recorded} observations, "
                f"{investigating} sent for research, {skipped} skipped"
            )

        return persist


def _brief(domain: dict, recent: list[dict]) -> str:
    lines = [f"Domain: {domain['name']}"]
    if domain.get("description"):
        lines.append(domain["description"])
    lines.append("\nAlready observed (most recent first):")
    lines += [f"- {o['statement']}" for o in recent] or ["- nothing yet"]
    return "\n".join(lines)


def _search(
    ctx: Context, queries: list[str], sources: list[str]
) -> tuple[list[SearchResult], dict[str, str]]:
    """Distinct recent results across queries, and the query that found each.

    The Scout uses the domain's discovery sources (the default search when
    none are set). It looks for what is new, so it searches within a window
    and drops anything dated before it: old announcements resurface in
    search results and would otherwise be recorded as current events.
    """
    days = ctx.settings.scout_recent_days
    cutoff = datetime.now(UTC) - timedelta(days=days)
    results: list[SearchResult] = []
    found_by: dict[str, str] = {}
    for query in queries:
        for result in ctx.acquisition.discover(
            query,
            max_results=ctx.settings.max_search_results,
            recent_days=days,
            sources=sources or None,
        ):
            published = parse_date(result.published_at)
            if result.url in found_by or (published and published < cutoff):
                continue
            found_by[result.url] = query
            results.append(result)
    return results, found_by


def _render_results(results: list[SearchResult]) -> str:
    parts = []
    for i, r in enumerate(results, 1):
        date = f" ({r.published_at})" if r.published_at else ""
        parts.append(f"[R{i}] {r.title}{date}\n{r.url}\n{r.snippet}")
    return "\n\n".join(parts)


def _key(statement: str) -> str:
    return " ".join(statement.lower().split())
