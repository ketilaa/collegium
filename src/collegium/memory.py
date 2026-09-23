"""Reading and writing institutional memory.

Write functions take a connection inside `Database.acting_as`, so the schema
records who wrote what. Nothing here commits; callers own the transaction.
"""

import hashlib
from collections.abc import Iterable
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


def critiques_of(conn: Connection, target_id: UUID) -> list[dict]:
    return conn.execute(
        "SELECT k.*, n.created_at FROM critiques k JOIN nodes n ON n.id = k.id "
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
# Helpers
# ---------------------------------------------------------------------------


def _unit(value: float | None) -> float | None:
    return None if value is None else round(min(1.0, max(0.0, float(value))), 3)


def _timestamp_or_none(value: str | None) -> str | None:
    moment = parse_date(value)
    return moment.isoformat() if moment else None
