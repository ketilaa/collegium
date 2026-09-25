"""Knowledge-gap detection: deterministic checks the Strategist starts from.

Each gap names what it concerns, so the Strategist can set a goal about it
and choose an action. The model decides what matters most; these rules
decide what counts as a gap.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from collegium import memory
from collegium.db import Connection
from collegium.grounding import mentions
from collegium.reliability import classify
from collegium.roles.historian import BLOCKING_SEVERITY, MIN_INDEPENDENT_SOURCES, site
from collegium.roles.resolver import MAX_ROUNDS

# A domain not scouted for this long is a gap in itself.
STALE_AFTER = timedelta(hours=20)
# An entity mentioned this often deserves attention of its own.
NOTABLE_MENTIONS = 2


@dataclass(frozen=True)
class Gap:
    # weak support | unresolved critiques | unstudied entity | not scouted
    # | unanswered question
    kind: str
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
    for q in memory.unanswered_questions(conn, domain_id)[:3]:
        lacking = (
            f" Memory lacks: {q['missing']}" if q["missing"] and q["missing"] != q["text"] else ""
        )
        gaps.append(Gap("unanswered question", q["id"], f'the owner asked "{q["text"]}".{lacking}'))
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


# ---------------------------------------------------------------------------
# Sources worth following
# ---------------------------------------------------------------------------

# A site that produced this many observations or pieces of evidence in the
# domain within SOURCE_WINDOW is worth following through its feed.
MIN_SOURCE_USES = 2
SOURCE_WINDOW = timedelta(days=30)
# Kinds of site not proposed as sources: weak signals, or reached otherwise
# (Hacker News through its search feeds).
NOT_PROPOSED = frozenset(["social or video", "forum", "AI-agent forum"])


@dataclass(frozen=True)
class SourceCandidate:
    site: str
    homepage: str
    observations: int
    evidence: int

    @property
    def uses(self) -> int:
        return self.observations + self.evidence


def site_key(host: str) -> str:
    """The part of a host name a site is known by: rss.kode24.no and
    www.kode24.no are both kode24.no. (Two labels; country subdomains such as
    co.uk are not handled.)"""
    return ".".join(host.lower().split(".")[-2:])


def source_candidates(conn: Connection, domain_id: UUID) -> list[SourceCandidate]:
    """Sites that keep producing what the organization records in this
    domain, and that it does not follow yet or has not already proposed."""
    rows = conn.execute(
        "SELECT s.uri, 'observation' AS used_as FROM observations o "
        "JOIN nodes n ON n.id = o.id JOIN sources s ON s.id = o.source_id "
        "JOIN node_domains d ON d.node_id = o.id AND d.domain_id = %(d)s "
        "WHERE o.status <> 'dismissed' AND n.created_at > now() - %(w)s "
        "UNION ALL "
        "SELECT s.uri, 'evidence' FROM evidence e JOIN nodes n ON n.id = e.id "
        "JOIN sources s ON s.id = e.source_id "
        "JOIN node_domains d ON d.node_id = e.id AND d.domain_id = %(d)s "
        "WHERE n.created_at > now() - %(w)s",
        {"d": domain_id, "w": SOURCE_WINDOW},
    ).fetchall()
    followed = {
        site_key(urlsplit(r["url"]).hostname or "")
        for r in conn.execute(
            "SELECT url FROM approved_sources WHERE domain_id = %s AND status <> 'retired'",
            (domain_id,),
        )
    }
    proposed = {
        r["site"]
        for r in conn.execute(
            "SELECT details->>'site' AS site FROM decisions WHERE topic = 'source' "
            "AND details->>'domain_id' = %s",
            (str(domain_id),),
        )
    }
    counts: dict[str, dict] = {}
    for r in rows:
        parts = urlsplit(r["uri"])
        if not parts.hostname or classify(r["uri"])[0] in NOT_PROPOSED:
            continue
        key = site_key(parts.hostname)
        if key in followed or key in proposed:
            continue
        c = counts.setdefault(
            key,
            {"homepage": f"{parts.scheme}://{parts.hostname}", "observation": 0, "evidence": 0},
        )
        c[r["used_as"]] += 1
    candidates = [
        SourceCandidate(key, c["homepage"], c["observation"], c["evidence"])
        for key, c in counts.items()
    ]
    return sorted((c for c in candidates if c.uses >= MIN_SOURCE_USES), key=lambda c: -c.uses)
