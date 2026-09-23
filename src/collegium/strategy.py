"""Knowledge-gap detection: deterministic checks the Strategist starts from.

Each gap names what it concerns, so the Strategist can set a goal about it
and choose an action. The model decides what matters most; these rules
decide what counts as a gap.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from collegium import memory
from collegium.db import Connection
from collegium.grounding import mentions
from collegium.roles.historian import BLOCKING_SEVERITY, MIN_INDEPENDENT_SOURCES, site
from collegium.roles.resolver import MAX_ROUNDS

# A domain not scouted for this long is a gap in itself.
STALE_AFTER = timedelta(hours=20)
# An entity mentioned this often deserves attention of its own.
NOTABLE_MENTIONS = 2


@dataclass(frozen=True)
class Gap:
    kind: str  # weak support | unresolved critiques | unstudied entity | not scouted
    about: UUID | None  # the hypothesis or entity concerned
    description: str


def find_gaps(conn: Connection, domain_id: UUID) -> list[Gap]:
    gaps: list[Gap] = []
    hypotheses = memory.live_hypotheses(conn, [domain_id], limit=50)
    for h in hypotheses:
        if h["status"] == "accepted":
            continue
        sites = {site(u) for u in memory.supporting_source_uris(conn, h["id"])}
        if len(sites) < MIN_INDEPENDENT_SOURCES:
            gaps.append(
                Gap(
                    "weak support",
                    h["id"],
                    f"supported by {len(sites)} independent site(s); "
                    f"{MIN_INDEPENDENT_SOURCES} are needed",
                )
            )
        open_blocking = [
            c for c in memory.open_critiques(conn, h["id"]) if c["severity"] >= BLOCKING_SEVERITY
        ]
        if open_blocking and _loop_exhausted(conn, h["id"]):
            gaps.append(
                Gap(
                    "unresolved critiques",
                    h["id"],
                    f"{len(open_blocking)} serious critique(s) still open after "
                    f"{MAX_ROUNDS} rounds of resolution",
                )
            )

    statements = [h["statement"] for h in hypotheses]
    goal_targets = {t for g in memory.active_goals(conn, [domain_id]) for t in g["about"]}
    for e in memory.top_entities(conn, [domain_id], limit=20):
        if e["mentions"] < NOTABLE_MENTIONS or e["id"] in goal_targets:
            continue
        if not any(mentions(s, e["name"]) for s in statements):
            gaps.append(
                Gap(
                    "unstudied entity",
                    e["id"],
                    f"mentioned {e['mentions']} times, but no hypothesis is about it",
                )
            )

    last = last_scouted(conn, domain_id)
    if last is None or datetime.now(UTC) - last > STALE_AFTER:
        when = "never" if last is None else f"last on {last:%Y-%m-%d %H:%M}"
        gaps.append(Gap("not scouted", None, f"the domain was scouted {when}"))
    return gaps


def last_scouted(conn: Connection, domain_id: UUID) -> datetime | None:
    row = conn.execute(
        "SELECT max(created_at) AS at FROM jobs "
        "WHERE kind = 'scout' AND payload->>'domain_id' = %s",
        (str(domain_id),),
    ).fetchone()
    return row["at"]


def _loop_exhausted(conn: Connection, hypothesis_id: UUID) -> bool:
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE kind = 'resolve' AND status = 'succeeded' "
        "AND payload->>'hypothesis_id' = %s AND (payload->>'round')::int >= %s LIMIT 1",
        (str(hypothesis_id), MAX_ROUNDS),
    ).fetchone()
    return row is not None
