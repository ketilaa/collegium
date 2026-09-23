"""The Scout: breadth-first exploration of a domain, producing observations."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, Field

from collegium import jobs, memory
from collegium.acquisition import SearchResult, injection_signals, interleave
from collegium.dates import parse_date
from collegium.db import Connection
from collegium.grounding import locate_excerpt, unsupported_terms
from collegium.jobs import Job
from collegium.roles.base import (
    Context,
    NothingToWorkWith,
    Persist,
    Role,
    SearchPlan,
    flag_note,
)
from collegium.untrusted import fence, warning


class ProposedObservation(BaseModel):
    source: int = Field(description="Number of the search result it comes from, e.g. 3 for [R3]")
    quote: str = Field(
        description="Words copied exactly from that result that state what was observed"
    )
    statement: str = Field(
        description="One factual sentence saying what the quote says, with names, roles, "
        "numbers and dates exactly as in the quote"
    )
    occurred_at: str | None = Field(
        None, description="ISO date the event happened, if the result says"
    )
    why_it_matters: str
    investigate: bool


class ScoutReport(BaseModel):
    observations: list[ProposedObservation] = Field(max_length=5)


class ObservationCheck(BaseModel):
    number: int = Field(description="Number of the statement checked, e.g. 2 for [2]")
    supported: bool
    problem: str | None = Field(None, description="What the quote does not support, if any")


class ObservationChecks(BaseModel):
    checks: list[ObservationCheck]


@dataclass(frozen=True)
class Grounded:
    proposal: ProposedObservation
    result: SearchResult
    quote: str  # the lead's own words


def ground_observations(
    proposals: list[ProposedObservation], results: list[SearchResult]
) -> tuple[list[Grounded], dict[str, int]]:
    """Keep proposals whose quote is in the cited lead and whose statement
    introduces no name or number the quote and title lack."""
    kept: list[Grounded] = []
    dropped = {"no such result": 0, "quote not in result": 0, "unsupported names": 0}
    for p in proposals:
        if not 1 <= p.source <= len(results):
            dropped["no such result"] += 1
            continue
        result = results[p.source - 1]
        quote = locate_excerpt(p.quote, f"{result.title}\n{result.snippet}")
        if quote is None:
            dropped["quote not in result"] += 1
        elif unsupported_terms(p.statement, f"{result.title}\n{quote}"):
            dropped["unsupported names"] += 1
        else:
            kept.append(Grounded(p, result, quote))
    return kept, dropped


class Scout(Role):
    name = "scout"
    job_kind = "scout"
    prompt_file = "scout.md"

    def prepare(self, ctx: Context, job: Job) -> Persist:
        domain_id = UUID(job.payload["domain_id"])
        with ctx.db.reading() as conn:
            domain = memory.domain(conn, domain_id)
            recent = memory.recent_observations(conn, domain_id)
            feeds = memory.active_feeds(conn, domain_id)
            entities = memory.top_entities(conn, [domain_id])
        if domain is None:
            raise LookupError(f"domain {domain_id} not found")

        brief = _brief(domain, recent, entities)
        system = self.system_prompt()
        plan = ctx.llm.generate(
            system, brief + "\n\nWhich web searches should you run now?", SearchPlan
        )
        if ctx.acquisition is None:
            raise NothingToWorkWith("no acquisition provider configured")
        searched, found = _search(ctx, plan.queries, domain["discovery_sources"])
        crawled, crawled_from, feed_errors = _crawl(ctx, feeds)
        with ctx.db.reading() as conn:
            # Feeds repeat their items run after run; skip what is already known.
            seen = memory.known_source_uris(conn, [r.url for r in crawled])
        crawled = [r for r in crawled if r.url not in seen and r.url not in found]
        found |= crawled_from
        results = interleave([searched, crawled])
        if not results:
            raise NothingToWorkWith(f"no leads for {plan.queries} or from {len(feeds)} feeds")

        report = ctx.llm.generate(
            system,
            brief
            + "\n\nSearch results:\n\n"
            + _render_results(results)
            + "\n\nWhich observations should the organization record?",
            ScoutReport,
        )
        grounded, dropped = ground_observations(report.observations, results)
        grounded, dropped["failed check"] = _verify(ctx, system, grounded)
        known = {_key(o["statement"]) for o in recent}

        def persist(conn: Connection) -> str:
            recorded = investigating = skipped = 0
            for g in grounded:
                obs, result = g.proposal, g.result
                if _key(obs.statement) in known:
                    skipped += 1
                    continue
                known.add(_key(obs.statement))
                source_id = memory.record_source(
                    conn,
                    uri=result.url,
                    title=result.title,
                    published_at=result.published_at,
                    metadata={
                        "provider": result.provider,
                        **found[result.url],
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
                # The lead's own words, kept as evidence for the observation.
                evidence_id = memory.add_evidence(
                    conn,
                    summary=f"The source reports: {obs.statement}",
                    excerpt=g.quote,
                    source_id=source_id,
                    reliability=None,
                )
                memory.tag_domains(conn, evidence_id, [domain_id])
                memory.link_evidence(
                    conn,
                    evidence_id=evidence_id,
                    target_id=observation_id,
                    target_kind="observation",
                    stance="supports",
                    rationale="Quoted by the Scout when recording the observation.",
                )
                recorded += 1
                if obs.investigate:
                    jobs.enqueue(
                        conn, "research", {"observation_id": observation_id}, parent_job_id=job.id
                    )
                    investigating += 1
            rejected = ", ".join(f"{n} {why}" for why, n in dropped.items() if n) or "none"
            feed_note = f"; {len(crawled)} new feed items from {len(feeds)} feeds" if feeds else ""
            if feed_errors:
                feed_note += f" ({feed_errors} feeds failed)"
            return (
                f"queries={plan.queries}{feed_note}; recorded {recorded} observations, "
                f"{investigating} sent for research, {skipped} already known; "
                f"rejected: {rejected}." + flag_note(results)
            )

        return persist


def _verify(ctx: Context, system: str, grounded: list[Grounded]) -> tuple[list[Grounded], int]:
    """Ask the model whether each quote really states its statement. The
    cheap checks cannot tell a wrong association of names ("Anthropic CEO
    Sam Altman") from a right one. A statement without a verdict fails."""
    if not grounded:
        return [], 0
    listing = "\n\n".join(
        f"[{i}] Statement: {g.proposal.statement}\nQuote:\n{fence(f'Q{i}', g.quote)}"
        for i, g in enumerate(grounded, 1)
    )
    checks = ctx.llm.generate(
        system,
        "Check each statement against its quote. A statement is supported only if "
        "the quote itself says it: every name, role, organisation, number and date "
        "must match, and nothing may be added.\n\n" + listing,
        ObservationChecks,
    )
    supported = {c.number for c in checks.checks if c.supported}
    kept = [g for i, g in enumerate(grounded, 1) if i in supported]
    return kept, len(grounded) - len(kept)


def _brief(domain: dict, recent: list[dict], entities: list[dict]) -> str:
    lines = [f"Domain: {domain['name']}"]
    if domain.get("description"):
        lines.append(domain["description"])
    if entities:
        # What the organization keeps running into: starting points for
        # exploring what is related to them.
        lines.append("\nMost-mentioned in this domain so far:")
        lines += [f"- {e['name']} ({e['entity_type']}, {e['mentions']} mentions)" for e in entities]
    lines.append("\nAlready observed (most recent first):")
    lines += [f"- {o['statement']}" for o in recent] or ["- nothing yet"]
    return "\n".join(lines)


def _recent(ctx: Context, result: SearchResult) -> bool:
    published = parse_date(result.published_at)
    return not published or published >= datetime.now(UTC) - timedelta(
        days=ctx.settings.scout_recent_days
    )


def _crawl(ctx: Context, feeds: list[dict]) -> tuple[list[SearchResult], dict[str, dict], int]:
    """Recent items from the domain's approved feeds, where each came from,
    and how many feeds failed. A failing feed is skipped (its failed call is
    recorded), so one broken feed does not stop the Scout."""
    items: list[SearchResult] = []
    came_from: dict[str, dict] = {}
    errors = 0
    for feed in feeds:
        try:
            crawled = ctx.acquisition.crawl(feed["url"], max_items=ctx.settings.max_feed_items)
        except Exception:
            errors += 1
            continue
        for item in crawled:
            if item.url not in came_from and _recent(ctx, item):
                came_from[item.url] = {"feed": feed["url"]}
                items.append(item)
    return items, came_from, errors


def _search(
    ctx: Context, queries: list[str], sources: list[str]
) -> tuple[list[SearchResult], dict[str, dict]]:
    """Distinct recent results across queries, and the query that found each.

    The Scout uses the domain's discovery sources (the default search when
    none are set). It looks for what is new, so it searches within a window
    and drops anything dated before it: old announcements resurface in
    search results and would otherwise be recorded as current events.
    """
    results: list[SearchResult] = []
    found_by: dict[str, dict] = {}
    for query in queries:
        for result in ctx.acquisition.discover(
            query,
            max_results=ctx.settings.max_search_results,
            recent_days=ctx.settings.scout_recent_days,
            sources=sources or None,
        ):
            if result.url in found_by or not _recent(ctx, result):
                continue
            found_by[result.url] = {"query": query}
            results.append(result)
    return results, found_by


def _render_results(results: list[SearchResult]) -> str:
    """Leads as fenced blocks, labelled R1, R2, ..."""
    parts = []
    for i, r in enumerate(results, 1):
        date = f" ({r.published_at})" if r.published_at else ""
        block = fence(f"R{i}", f"{r.title}{date}\n{r.url}\n{r.snippet}")
        note = warning(injection_signals(r))
        parts.append(f"[R{i}]\n{block}" + (f"\n{note}" if note else ""))
    return "\n\n".join(parts)


def _key(statement: str) -> str:
    return " ".join(statement.lower().split())
