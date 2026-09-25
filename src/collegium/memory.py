"""Reading and writing institutional memory.

Write functions take a connection inside `Database.acting_as`, so the schema
records who wrote what. Nothing here commits; callers own the transaction.
"""

import hashlib
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from collegium.dates import parse_date
from collegium.db import Connection

# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _insert_node(conn: Connection, kind: str, table: str, values: dict[str, Any]) -> UUID:
    """Insert a node and its subtype row in one statement."""
    columns = list(values)
    query = sql.SQL(
        "WITH n AS (INSERT INTO nodes (kind) VALUES (%s) RETURNING id) "
        "INSERT INTO {table} (id, {columns}) SELECT n.id, {placeholders} FROM n RETURNING id"
    ).format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(map(sql.Identifier, columns)),
        placeholders=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
    )
    return conn.execute(query, (kind, *values.values())).fetchone()["id"]


def tag_domains(conn: Connection, node_id: UUID, domain_ids: Iterable[UUID]) -> None:
    for domain_id in domain_ids:
        conn.execute(
            "INSERT INTO node_domains (node_id, domain_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (node_id, domain_id),
        )


def record_source(
    conn: Connection,
    *,
    uri: str,
    title: str | None = None,
    published_at: str | None = None,
    content: str | None = None,
    metadata: dict[str, Any] | None = None,
    acquisition_id: UUID | None = None,
) -> UUID:
    """Return the source for this uri and content, creating it if new.

    A changed page gets a new source row, so evidence keeps pointing at the
    version it was taken from.
    """
    digest = hashlib.sha256(content.encode()).hexdigest() if content else None
    row = conn.execute(
        "SELECT id FROM sources WHERE uri = %s AND content_sha256 IS NOT DISTINCT FROM %s "
        "ORDER BY retrieved_at DESC LIMIT 1",
        (uri, digest),
    ).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO sources (uri, title, published_at, content_sha256, metadata, acquisition_id) "
        "VALUES (%s, %s, %s::timestamptz, %s, %s, %s) RETURNING id",
        (
            uri,
            title,
            _timestamp_or_none(published_at),
            digest,
            Jsonb(metadata or {}),
            acquisition_id,
        ),
    ).fetchone()["id"]


def add_observation(
    conn: Connection,
    *,
    statement: str,
    source_id: UUID | None,
    recommendation: str | None,
    status: str = "proposed",
    occurred_at: str | None = None,
) -> UUID:
    return _insert_node(
        conn,
        "observation",
        "observations",
        {
            "statement": statement,
            "source_id": source_id,
            "recommendation": recommendation,
            "status": status,
            "occurred_at": _timestamp_or_none(occurred_at),
        },
    )


def add_hypothesis(conn: Connection, *, statement: str, rationale: str | None) -> UUID:
    return _insert_node(
        conn, "hypothesis", "hypotheses", {"statement": statement, "rationale": rationale}
    )


def add_evidence(
    conn: Connection,
    *,
    summary: str,
    excerpt: str,
    source_id: UUID,
    reliability: float | None,
) -> UUID:
    return _insert_node(
        conn,
        "evidence",
        "evidence",
        {
            "summary": summary,
            "excerpt": excerpt,
            "source_id": source_id,
            "reliability": _unit(reliability),
        },
    )


def add_critique(
    conn: Connection,
    *,
    target_id: UUID,
    argument: str,
    alternative_explanation: str | None,
    severity: int,
) -> UUID:
    return _insert_node(
        conn,
        "critique",
        "critiques",
        {
            "target_id": target_id,
            "argument": argument,
            "alternative_explanation": alternative_explanation,
            "severity": min(5, max(1, severity)),
        },
    )


def add_relationship(
    conn: Connection,
    *,
    subject_id: UUID,
    predicate: str,
    object_id: UUID,
    rationale: str | None = None,
) -> UUID:
    return _insert_node(
        conn,
        "relationship",
        "relationships",
        {
            "subject_id": subject_id,
            "predicate": predicate,
            "object_id": object_id,
            "rationale": rationale,
        },
    )


def link_evidence(
    conn: Connection,
    *,
    evidence_id: UUID,
    target_id: UUID,
    target_kind: str,
    stance: str,
    rationale: str | None,
    weight: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO evidence_links "
        "(evidence_id, target_id, target_kind, stance, rationale, weight) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (evidence_id, target_id, target_kind, stance, rationale, _unit(weight)),
    )


def assess_confidence(
    conn: Connection, *, target_id: UUID, target_kind: str, confidence: float, rationale: str
) -> None:
    conn.execute(
        "INSERT INTO confidence_assessments (target_id, target_kind, confidence, rationale) "
        "VALUES (%s, %s, %s, %s)",
        (target_id, target_kind, _unit(confidence), rationale),
    )


def record_acquisition(
    conn: Connection,
    *,
    capability: str,
    provider: str,
    request: dict[str, Any],
    result_count: int,
    error: str | None,
) -> UUID:
    return conn.execute(
        "INSERT INTO acquisitions (capability, provider, request, result_count, error) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (capability, provider, Jsonb(request), result_count, error),
    ).fetchone()["id"]


def paid_calls_in_window(
    conn: Connection, providers: list[str], hours: int = 24
) -> tuple[int, datetime | None]:
    """How many calls to these providers were made in the last `hours`, and
    when the oldest of them was made (the window frees up 24 hours after)."""
    row = conn.execute(
        "SELECT count(*) AS n, min(requested_at) AS oldest FROM acquisitions "
        "WHERE provider = ANY(%s) AND requested_at > now() - make_interval(hours => %s)",
        (providers, hours),
    ).fetchone()
    return row["n"], row["oldest"]


def add_goal(
    conn: Connection,
    *,
    statement: str,
    success_criteria: str | None,
    priority: int,
    program_id: UUID | None = None,
) -> UUID:
    return _insert_node(
        conn,
        "goal",
        "goals",
        {
            "statement": statement,
            "success_criteria": success_criteria,
            "priority": min(5, max(1, priority)),
            "program_id": program_id,
            "status": "active",
        },
    )


def set_goal_status(
    conn: Connection, goal_id: UUID, status: str, outcome: str | None = None
) -> None:
    """Change a goal's status; a closed goal keeps why it was closed."""
    conn.execute(
        "UPDATE goals SET status = %s, outcome = coalesce(%s, outcome) WHERE id = %s",
        (status, outcome, goal_id),
    )


def closed_goals(
    conn: Connection, domain_ids: list[UUID], status: str, limit: int = 5
) -> list[dict]:
    """Goals most recently closed with this status, newest first."""
    return conn.execute(
        "SELECT g.id, g.statement, g.outcome, max(l.at) AS closed_at FROM goals g "
        "JOIN node_domains d ON d.node_id = g.id "
        "JOIN audit_log l ON l.table_name = 'goals' AND l.row_id = g.id "
        "AND l.new_row ->> 'status' = g.status "
        "WHERE g.status = %s AND d.domain_id = ANY(%s) "
        "GROUP BY g.id ORDER BY closed_at DESC LIMIT %s",
        (status, domain_ids, limit),
    ).fetchall()


def add_program(conn: Connection, *, name: str, charter: str) -> UUID:
    """A research program, proposed: the owner decides whether it opens."""
    return _insert_node(
        conn, "program", "programs", {"name": name, "charter": charter, "status": "proposed"}
    )


def add_decision(conn: Connection, *, statement: str, rationale: str) -> UUID:
    return _insert_node(
        conn, "decision", "decisions", {"statement": statement, "rationale": rationale}
    )


def set_mission(conn: Connection, statement: str, domain_id: UUID | None) -> UUID:
    """The owner's mission for the organization (no domain) or one domain:
    an approved decision that supersedes the mission before it."""
    previous = mission(conn, domain_id)
    actor = conn.execute("SELECT current_actor() AS id").fetchone()["id"]
    mission_id = _insert_node(
        conn,
        "decision",
        "decisions",
        {
            "statement": statement,
            "rationale": "The owner's mission.",
            "topic": "mission",
            "status": "approved",
            "resolved_by": actor,
            "resolved_at": datetime.now(UTC),
        },
    )
    if domain_id is not None:
        tag_domains(conn, mission_id, [domain_id])
    if previous is not None:
        conn.execute(
            "UPDATE decisions SET status = 'superseded', superseded_by = %s WHERE id = %s",
            (mission_id, previous["id"]),
        )
    return mission_id


def missions(conn: Connection, domain_id: UUID | None) -> list[dict]:
    """Every mission set for the organization (no domain) or a domain,
    newest first; the first is current unless it was superseded."""
    return conn.execute(
        "SELECT d.* FROM decisions d WHERE d.topic = 'mission' "
        "AND d.status IN ('approved', 'superseded') AND "
        + (
            "EXISTS (SELECT 1 FROM node_domains n WHERE n.node_id = d.id AND n.domain_id = %s) "
            if domain_id is not None
            else "NOT EXISTS (SELECT 1 FROM node_domains n WHERE n.node_id = d.id) "
        )
        + "ORDER BY d.resolved_at DESC",
        (domain_id,) if domain_id is not None else (),
    ).fetchall()


def mission(conn: Connection, domain_id: UUID | None) -> dict | None:
    """The current mission for the organization (no domain) or a domain."""
    current = [m for m in missions(conn, domain_id) if m["status"] == "approved"]
    return current[0] if current else None


def active_goals(conn: Connection, domain_ids: list[UUID]) -> list[dict]:
    """Active goals in these domains, each with the ids of what it is about."""
    return conn.execute(
        "SELECT g.id, g.statement, g.success_criteria, g.priority, n.created_at, "
        "coalesce(array_agg(r.object_id) FILTER (WHERE r.object_id IS NOT NULL), '{}') "
        "AS about FROM goals g JOIN nodes n ON n.id = g.id "
        "JOIN node_domains d ON d.node_id = g.id "
        "LEFT JOIN relationships r ON r.subject_id = g.id AND r.predicate = 'investigates' "
        "AND r.retracted_at IS NULL "
        "WHERE g.status = 'active' AND d.domain_id = ANY(%s) "
        "GROUP BY g.id, n.created_at ORDER BY g.priority, n.created_at, g.id",
        (domain_ids,),
    ).fetchall()


def programs(conn: Connection, domain_ids: list[UUID]) -> list[dict]:
    return conn.execute(
        "SELECT p.* FROM programs p JOIN node_domains d ON d.node_id = p.id "
        "WHERE d.domain_id = ANY(%s) AND p.status <> 'closed' ORDER BY p.priority",
        (domain_ids,),
    ).fetchall()


def set_observation_status(conn: Connection, observation_id: UUID, status: str) -> None:
    conn.execute("UPDATE observations SET status = %s WHERE id = %s", (status, observation_id))


def set_hypothesis_status(
    conn: Connection, hypothesis_id: UUID, status: str, superseded_by: UUID | None = None
) -> None:
    conn.execute(
        "UPDATE hypotheses SET status = %s, superseded_by = %s WHERE id = %s",
        (status, superseded_by, hypothesis_id),
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

LIVE_HYPOTHESIS_STATUSES = ("proposed", "under_review", "accepted")


def domain_by_slug(conn: Connection, slug: str) -> dict | None:
    return conn.execute("SELECT * FROM domains WHERE slug = %s", (slug,)).fetchone()


def domain(conn: Connection, domain_id: UUID) -> dict | None:
    return conn.execute("SELECT * FROM domains WHERE id = %s", (domain_id,)).fetchone()


def active_domains(conn: Connection) -> list[dict]:
    return conn.execute("SELECT * FROM domains WHERE status = 'active' ORDER BY slug").fetchall()


def active_feeds(conn: Connection, domain_id: UUID) -> list[dict]:
    return conn.execute(
        "SELECT * FROM approved_sources WHERE domain_id = %s AND kind = 'feed' "
        "AND status = 'active' ORDER BY created_at",
        (domain_id,),
    ).fetchall()


def known_source_uris(conn: Connection, uris: list[str]) -> set[str]:
    rows = conn.execute("SELECT DISTINCT uri FROM sources WHERE uri = ANY(%s)", (uris,))
    return {r["uri"] for r in rows}


def find_entity(conn: Connection, name: str) -> UUID | None:
    """An active entity known by this name or alias, ignoring case."""
    row = conn.execute(
        "SELECT id FROM entities WHERE status = 'active' "
        "AND (lower(name) = lower(%s) OR lower(%s) = ANY(SELECT lower(a) FROM unnest(aliases) a)) "
        "LIMIT 1",
        (name, name),
    ).fetchone()
    return row["id"] if row else None


def add_entity(conn: Connection, *, name: str, entity_type: str) -> UUID:
    return _insert_node(conn, "entity", "entities", {"name": name, "entity_type": entity_type})


def top_entities(conn: Connection, domain_ids: list[UUID], limit: int = 10) -> list[dict]:
    """The entities mentioned most often in these domains."""
    return conn.execute(
        "SELECT e.id, e.name, e.entity_type, count(*) AS mentions FROM entities e "
        "JOIN relationships r ON r.object_id = e.id AND r.predicate = 'mentions' "
        "AND r.retracted_at IS NULL "
        "JOIN node_domains d ON d.node_id = e.id "
        "WHERE e.status = 'active' AND d.domain_id = ANY(%s) "
        "GROUP BY e.id ORDER BY mentions DESC, e.name LIMIT %s",
        (domain_ids, limit),
    ).fetchall()


def evidence_texts(conn: Connection, evidence_ids: list[UUID]) -> list[dict]:
    return conn.execute(
        "SELECT id, excerpt FROM evidence WHERE id = ANY(%s) ORDER BY id", (evidence_ids,)
    ).fetchall()


def observation_evidence(conn: Connection, observation_id: UUID) -> list[dict]:
    """Evidence attached to an observation, such as the Scout's quote."""
    return conn.execute(
        "SELECT e.id, e.excerpt FROM evidence_links l JOIN evidence e ON e.id = l.evidence_id "
        "WHERE l.target_id = %s AND l.retracted_at IS NULL",
        (observation_id,),
    ).fetchall()


def node_domain_ids(conn: Connection, node_id: UUID) -> list[UUID]:
    rows = conn.execute("SELECT domain_id FROM node_domains WHERE node_id = %s", (node_id,))
    return [r["domain_id"] for r in rows]


def recent_observations(conn: Connection, domain_id: UUID, limit: int = 20) -> list[dict]:
    return conn.execute(
        "SELECT o.id, o.statement, o.status, n.created_at FROM observations o "
        "JOIN nodes n ON n.id = o.id JOIN node_domains d ON d.node_id = o.id "
        "WHERE d.domain_id = %s ORDER BY n.created_at DESC LIMIT %s",
        (domain_id, limit),
    ).fetchall()


def observation(conn: Connection, observation_id: UUID) -> dict | None:
    return conn.execute(
        "SELECT o.*, s.uri AS source_uri, s.title AS source_title FROM observations o "
        "LEFT JOIN sources s ON s.id = o.source_id WHERE o.id = %s",
        (observation_id,),
    ).fetchone()


def live_hypotheses(conn: Connection, domain_ids: list[UUID], limit: int = 15) -> list[dict]:
    """Hypotheses still in play in these domains, most confident first."""
    return conn.execute(
        "SELECT DISTINCT h.id, h.statement, h.status, c.confidence FROM hypotheses h "
        "JOIN node_domains d ON d.node_id = h.id "
        "LEFT JOIN current_confidence c ON c.target_id = h.id "
        "WHERE d.domain_id = ANY(%s) AND h.status = ANY(%s) "
        "ORDER BY c.confidence DESC NULLS LAST LIMIT %s",
        (domain_ids, list(LIVE_HYPOTHESIS_STATUSES), limit),
    ).fetchall()


def statement_key(statement: str) -> str:
    """A statement compared loosely: case, spacing and a final period ignored."""
    return " ".join(statement.lower().split()).rstrip(" .")


def find_live_hypothesis(conn: Connection, statement: str, domain_ids: list[UUID]) -> UUID | None:
    """A live hypothesis in these domains with the same statement, if any."""
    row = conn.execute(
        "SELECT h.id FROM hypotheses h JOIN node_domains d ON d.node_id = h.id "
        "WHERE d.domain_id = ANY(%s) AND h.status = ANY(%s) "
        "AND rtrim(lower(regexp_replace(btrim(h.statement), '\\s+', ' ', 'g')), ' .') = %s "
        "LIMIT 1",
        (domain_ids, list(LIVE_HYPOTHESIS_STATUSES), statement_key(statement)),
    ).fetchone()
    return row["id"] if row else None


def has_relationship(conn: Connection, subject_id: UUID, predicate: str, object_id: UUID) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM relationships WHERE subject_id = %s AND predicate = %s "
            "AND object_id = %s AND retracted_at IS NULL",
            (subject_id, predicate, object_id),
        ).fetchone()
        is not None
    )


def hypothesis(conn: Connection, hypothesis_id: UUID) -> dict | None:
    return conn.execute(
        "SELECT h.*, c.confidence FROM hypotheses h "
        "LEFT JOIN current_confidence c ON c.target_id = h.id WHERE h.id = %s",
        (hypothesis_id,),
    ).fetchone()


def hypothesis_evidence(conn: Connection, hypothesis_id: UUID) -> list[dict]:
    return conn.execute(
        "SELECT e.id, e.summary, e.excerpt, e.reliability, l.stance, l.rationale, "
        "s.uri AS source_uri FROM evidence_links l "
        "JOIN evidence e ON e.id = l.evidence_id LEFT JOIN sources s ON s.id = e.source_id "
        "WHERE l.target_id = %s AND l.retracted_at IS NULL ORDER BY l.created_at",
        (hypothesis_id,),
    ).fetchall()


def supporting_source_uris(conn: Connection, hypothesis_id: UUID) -> list[str]:
    """Where the active supporting evidence for a hypothesis comes from."""
    rows = conn.execute(
        "SELECT DISTINCT s.uri FROM evidence_links l JOIN evidence e ON e.id = l.evidence_id "
        "JOIN sources s ON s.id = e.source_id "
        "WHERE l.target_id = %s AND l.stance = 'supports' AND l.retracted_at IS NULL",
        (hypothesis_id,),
    )
    return [r["uri"] for r in rows if r["uri"]]


def citations(conn: Connection, target_id: UUID) -> list[dict]:
    """The evidence for or against a record, each with its source and the
    external call that found the source: the full chain from belief to
    where the words came from and how the organization came across them."""
    return conn.execute(
        "SELECT e.id, e.summary, e.excerpt, e.reliability, l.stance, "
        "s.uri, s.title AS source_title, s.published_at, s.retrieved_at, s.metadata, "
        "q.capability, q.provider, q.request, q.requested_at, fa.name AS found_by "
        "FROM evidence_links l JOIN evidence e ON e.id = l.evidence_id "
        "LEFT JOIN sources s ON s.id = e.source_id "
        "LEFT JOIN acquisitions q ON q.id = s.acquisition_id "
        "LEFT JOIN actors fa ON fa.id = q.requested_by "
        "WHERE l.target_id = %s AND l.retracted_at IS NULL ORDER BY l.created_at",
        (target_id,),
    ).fetchall()


def open_critiques(conn: Connection, target_id: UUID) -> list[dict]:
    return conn.execute(
        "SELECT k.*, a.name AS raised_by FROM critiques k JOIN nodes n ON n.id = k.id "
        "JOIN actors a ON a.id = n.created_by "
        "WHERE k.target_id = %s AND k.status = 'open' ORDER BY n.created_at",
        (target_id,),
    ).fetchall()


def set_critique_status(
    conn: Connection, critique_id: UUID, status: str, resolution: str | None
) -> None:
    conn.execute(
        "UPDATE critiques SET status = %s, resolution = %s WHERE id = %s",
        (status, resolution, critique_id),
    )


def critiques_of(conn: Connection, target_id: UUID) -> list[dict]:
    return conn.execute(
        "SELECT k.*, n.created_at, a.name AS raised_by FROM critiques k "
        "JOIN nodes n ON n.id = k.id JOIN actors a ON a.id = n.created_by "
        "WHERE k.target_id = %s ORDER BY n.created_at",
        (target_id,),
    ).fetchall()


def confidence_history(conn: Connection, target_id: UUID) -> list[dict]:
    return conn.execute(
        "SELECT c.confidence, c.rationale, c.assessed_at, a.name AS assessed_by "
        "FROM confidence_assessments c JOIN actors a ON a.id = c.assessed_by "
        "WHERE c.target_id = %s ORDER BY c.assessed_at",
        (target_id,),
    ).fetchall()


def refined_hypotheses(conn: Connection, hypothesis_id: UUID) -> list[UUID]:
    """Hypotheses that this one was proposed to refine."""
    rows = conn.execute(
        "SELECT object_id FROM relationships "
        "WHERE subject_id = %s AND predicate = 'refines' AND retracted_at IS NULL",
        (hypothesis_id,),
    )
    return [r["object_id"] for r in rows]


# ---------------------------------------------------------------------------
# Questions from the owner
# ---------------------------------------------------------------------------


def add_question(conn: Connection, text: str, domain_id: UUID | None = None) -> UUID:
    question_id = _insert_node(conn, "question", "questions", {"text": text})
    if domain_id is not None:
        tag_domains(conn, question_id, [domain_id])
    return question_id


def question(conn: Connection, question_id: UUID) -> dict | None:
    return conn.execute("SELECT * FROM questions WHERE id = %s", (question_id,)).fetchone()


def answer_question(
    conn: Connection,
    question_id: UUID,
    *,
    answer: str,
    answered: bool,
    missing: str | None,
    cites: list[UUID],
) -> None:
    conn.execute(
        "UPDATE questions SET status = %s, answer = %s, missing = %s, answered_at = now() "
        "WHERE id = %s",
        ("answered" if answered else "unanswered", answer, missing, question_id),
    )
    # The answer refers to its sources as [1], [2], ... in this order.
    for n, node_id in enumerate(dict.fromkeys(cites), 1):
        add_relationship(
            conn, subject_id=question_id, predicate="cites", object_id=node_id, rationale=f"[{n}]"
        )


def unanswered_questions(conn: Connection, domain_id: UUID, days: int = 14) -> list[dict]:
    """Recent questions memory could not answer, asked about this domain or
    about no domain in particular."""
    return conn.execute(
        "SELECT q.*, n.created_at FROM questions q JOIN nodes n ON n.id = q.id "
        "WHERE q.status = 'unanswered' AND n.created_at > now() - make_interval(days => %s) "
        "AND (NOT EXISTS (SELECT 1 FROM node_domains d WHERE d.node_id = q.id) "
        "     OR EXISTS (SELECT 1 FROM node_domains d WHERE d.node_id = q.id "
        "                AND d.domain_id = %s)) "
        "ORDER BY n.created_at DESC",
        (days, domain_id),
    ).fetchall()


def recall(conn: Connection, terms: list[str], domain_id: UUID | None = None) -> dict:
    """What memory holds about these search terms, best matches first.

    Full-text search: statements and summaries are English (the working
    language); excerpts and names are matched word for word, since they
    keep the source's language. A record matches a term when it has all the
    term's words, in any order; any term will do, and ranking puts records
    matching more of them first.
    """
    terms = _search_terms(terms)
    if not terms:
        return {"hypotheses": [], "observations": [], "evidence": [], "entities": []}
    in_domain = (
        "AND (%(domain)s::uuid IS NULL OR EXISTS (SELECT 1 FROM node_domains d "
        "WHERE d.node_id = {id} AND d.domain_id = %(domain)s))"
    )
    params: dict[str, Any] = {"domain": domain_id} | {f"t{i}": t for i, t in enumerate(terms)}

    def any_term(config: str) -> str:
        return (
            "("
            + " || ".join(f"plainto_tsquery('{config}', %(t{i})s)" for i in range(len(terms)))
            + ")"
        )

    english, simple = any_term("english"), any_term("simple")
    return {
        "hypotheses": conn.execute(
            "SELECT h.id, h.statement, h.status, c.confidence, "
            f"ts_rank(to_tsvector('english', h.statement || ' ' || coalesce(h.rationale, '')), "
            f"{english}) AS rank FROM hypotheses h "
            "LEFT JOIN current_confidence c ON c.target_id = h.id "
            f"WHERE to_tsvector('english', h.statement || ' ' || coalesce(h.rationale, '')) "
            f"@@ {english} {in_domain.format(id='h.id')} "
            "ORDER BY h.status IN ('rejected', 'superseded', 'retired'), rank DESC LIMIT 8",
            params,
        ).fetchall(),
        "observations": conn.execute(
            "SELECT o.id, o.statement, o.status, n.created_at FROM observations o "
            "JOIN nodes n ON n.id = o.id WHERE to_tsvector('english', o.statement || ' ' || "
            f"coalesce(o.recommendation, '')) @@ {english} {in_domain.format(id='o.id')} "
            "ORDER BY ts_rank(to_tsvector('english', o.statement), "
            f"{english}) DESC, n.created_at DESC LIMIT 6",
            params,
        ).fetchall(),
        # Each piece of evidence once, with the first record it bears on
        # (hypotheses before critiques and observations).
        "evidence": conn.execute(
            "SELECT * FROM (SELECT DISTINCT ON (e.id) e.id, e.summary, e.excerpt, "
            "e.reliability, l.stance, l.target_id, l.target_kind, s.uri, "
            f"ts_rank(to_tsvector('english', e.summary), {english}) AS rank FROM evidence e "
            "JOIN evidence_links l ON l.evidence_id = e.id AND l.retracted_at IS NULL "
            "LEFT JOIN sources s ON s.id = e.source_id "
            f"WHERE (to_tsvector('english', e.summary) @@ {english} "
            f"OR to_tsvector('simple', e.excerpt) @@ {simple}) "
            f"{in_domain.format(id='e.id')} "
            "ORDER BY e.id, l.target_kind <> 'hypothesis', l.created_at) found "
            "ORDER BY rank DESC LIMIT 6",
            params,
        ).fetchall(),
        "entities": conn.execute(
            "SELECT e.id, e.name, e.entity_type, count(r.id) AS mentions FROM entities e "
            "LEFT JOIN relationships r ON r.object_id = e.id AND r.predicate = 'mentions' "
            "AND r.retracted_at IS NULL "
            f"WHERE e.status = 'active' AND to_tsvector('simple', e.name) @@ {simple} "
            "GROUP BY e.id ORDER BY mentions DESC LIMIT 8",
            params,
        ).fetchall(),
    }


def _search_terms(terms: list[str]) -> list[str]:
    """Search terms as plain words: quotes and operators removed."""
    cleaned = []
    for t in terms:
        t = " ".join(re.sub(r"[\"'()|&!:*<>-]", " ", t).split())
        if t and t.lower() not in ("or", "and", "not"):
            cleaned.append(t)
    return cleaned[:12]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit(value: float | None) -> float | None:
    return None if value is None else round(min(1.0, max(0.0, float(value))), 3)


def _timestamp_or_none(value: str | None) -> str | None:
    moment = parse_date(value)
    return moment.isoformat() if moment else None
