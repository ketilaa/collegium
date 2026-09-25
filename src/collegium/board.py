"""What the board sees: read-only queries over memory, shared by the CLI and
the web board so both show the same thing."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from collegium import memory
from collegium.db import Connection
from collegium.roles.historian import ACCEPT_AT, BLOCKING_SEVERITY
from collegium.roles.resolver import MAX_ROUNDS

LIVE = ("proposed", "under_review", "accepted")


def match_nodes(conn: Connection, prefix: str, kinds: list[str]) -> list[dict]:
    """Nodes of these kinds whose id starts with `prefix`."""
    return conn.execute(
        "SELECT id, kind FROM nodes WHERE kind = ANY(%s) AND id::text LIKE %s",
        (kinds, prefix.lower() + "%"),
    ).fetchall()


def domains(conn: Connection) -> list[dict]:
    return conn.execute(
        "SELECT id, slug, name, description, status, discovery_sources FROM domains ORDER BY slug"
    ).fetchall()


# ---------------------------------------------------------------------------
# Hypotheses and observations
# ---------------------------------------------------------------------------


def hypotheses(conn: Connection, *, include_all: bool = False) -> list[dict]:
    where = "" if include_all else "WHERE h.status = ANY(%(live)s)"
    return conn.execute(
        "SELECT h.*, "
        "(SELECT array_agg(d.slug ORDER BY d.slug) FROM node_domains nd "
        " JOIN domains d ON d.id = nd.domain_id WHERE nd.node_id = h.id) AS domains "
        f"FROM hypothesis_overview h {where} "
        "ORDER BY h.current_confidence DESC NULLS LAST, h.created_at DESC",
        {"live": list(LIVE)},
    ).fetchall()


def hypothesis_detail(conn: Connection, hid: UUID) -> dict | None:
    """Everything that explains a hypothesis: who proposed it, where it came
    from, how its status and confidence changed, its evidence and critiques."""
    h = conn.execute(
        "SELECT h.*, a.name AS proposer, x.rationale, x.superseded_by "
        "FROM hypothesis_overview h JOIN actors a ON a.id = h.proposed_by "
        "JOIN hypotheses x ON x.id = h.id WHERE h.id = %s",
        (hid,),
    ).fetchone()
    if h is None:
        return None
    return {
        "hypothesis": h,
        "statuses": conn.execute(
            "SELECT s.at, s.from_status, s.to_status, a.name FROM hypothesis_status_history s "
            "JOIN actors a ON a.id = s.actor_id WHERE s.hypothesis_id = %s ORDER BY s.at",
            (hid,),
        ).fetchall(),
        "derived_from": conn.execute(
            "SELECT o.id, o.statement FROM relationships r "
            "JOIN observations o ON o.id = r.object_id WHERE r.subject_id = %s "
            "AND r.predicate = 'derived_from' AND r.retracted_at IS NULL",
            (hid,),
        ).fetchall(),
        "refines": _related_hypotheses(conn, "r.subject_id = %s", "r.object_id", hid),
        "refined_by": _related_hypotheses(conn, "r.object_id = %s", "r.subject_id", hid),
        "goals": conn.execute(
            "SELECT g.id, g.statement, g.status FROM relationships r "
            "JOIN goals g ON g.id = r.subject_id WHERE r.object_id = %s "
            "AND r.predicate = 'investigates' AND r.retracted_at IS NULL",
            (hid,),
        ).fetchall(),
        "confidence": memory.confidence_history(conn, hid),
        "citations": memory.citations(conn, hid),
        "critiques": critiques_of(conn, hid),
    }


def _related_hypotheses(conn: Connection, where: str, other: str, hid: UUID) -> list[dict]:
    return conn.execute(
        f"SELECT h.id, h.statement, h.status FROM relationships r JOIN hypotheses h "
        f"ON h.id = {other} WHERE {where} AND r.predicate = 'refines' "
        "AND r.retracted_at IS NULL",
        (hid,),
    ).fetchall()


def critiques_of(conn: Connection, target_id: UUID) -> list[dict]:
    """Critiques of a record, each with who raised it and who settled it."""
    return conn.execute(
        "SELECT k.*, n.created_at, a.name AS raised_by, "
        "(SELECT r.name FROM audit_log l JOIN actors r ON r.id = l.actor_id "
        " WHERE l.table_name = 'critiques' AND l.row_id = k.id AND l.action = 'UPDATE' "
        " AND l.new_row ->> 'status' <> 'open' ORDER BY l.at DESC LIMIT 1) AS settled_by "
        "FROM critiques k JOIN nodes n ON n.id = k.id JOIN actors a ON a.id = n.created_by "
        "WHERE k.target_id = %s ORDER BY n.created_at",
        (target_id,),
    ).fetchall()


def observation_detail(conn: Connection, oid: UUID) -> dict | None:
    o = conn.execute(
        "SELECT o.*, n.created_at, a.name AS recorded_by FROM observations o "
        "JOIN nodes n ON n.id = o.id JOIN actors a ON a.id = n.created_by WHERE o.id = %s",
        (oid,),
    ).fetchone()
    if o is None:
        return None
    return {
        "observation": o,
        "hypotheses": conn.execute(
            "SELECT h.id, h.statement, h.status FROM relationships r "
            "JOIN hypotheses h ON h.id = r.subject_id WHERE r.object_id = %s "
            "AND r.predicate = 'derived_from' AND r.retracted_at IS NULL",
            (oid,),
        ).fetchall(),
        "entities": conn.execute(
            "SELECT e.name, e.entity_type FROM relationships r "
            "JOIN entities e ON e.id = r.object_id WHERE r.subject_id = %s "
            "AND r.predicate = 'mentions' AND r.retracted_at IS NULL ORDER BY e.name",
            (oid,),
        ).fetchall(),
        "citations": memory.citations(conn, oid),
    }


def found_via(e: dict, local: Callable[[datetime], datetime]) -> str:
    """How the organization came across a source."""
    if e["capability"] is None:
        return "Found: not recorded"
    request = e["request"] or {}
    if e["capability"] == "crawl":
        how = f"reading approved feed {request.get('url')}"
    else:
        how = f'searching {e["provider"]} for "{request.get("query")}"'
    return f"Found by the {e['found_by']} {how} on {local(e['requested_at']):%Y-%m-%d %H:%M}"


# ---------------------------------------------------------------------------
# Direction: programs, goals and decisions
# ---------------------------------------------------------------------------


def goals(conn: Connection, *, include_all: bool = False) -> list[dict]:
    """Goals, each with how many jobs worked on it and what it investigates."""
    where = "" if include_all else "WHERE g.status = 'active'"
    rows = conn.execute(
        "SELECT g.*, n.created_at, p.name AS program, "
        "(SELECT count(*) FROM jobs j WHERE j.payload->>'goal_id' = g.id::text) AS jobs "
        "FROM goals g JOIN nodes n ON n.id = g.id LEFT JOIN programs p ON p.id = g.program_id "
        f"{where} ORDER BY g.status, g.priority, n.created_at"
    ).fetchall()
    about = _investigates(conn, [g["id"] for g in rows])
    for g in rows:
        g["about"] = about.get(g["id"], [])
    return rows


def _investigates(conn: Connection, goal_ids: list[UUID]) -> dict[UUID, list[dict]]:
    rows = conn.execute(
        "SELECT r.subject_id AS goal_id, t.id, t.kind, "
        "coalesce(h.statement, e.name, o.statement, k.argument) AS label, "
        "coalesce(h.status, e.status, o.status, k.status) AS status "
        "FROM relationships r JOIN nodes rn ON rn.id = r.id JOIN nodes t ON t.id = r.object_id "
        "LEFT JOIN hypotheses h ON h.id = t.id LEFT JOIN entities e ON e.id = t.id "
        "LEFT JOIN observations o ON o.id = t.id LEFT JOIN critiques k ON k.id = t.id "
        "WHERE r.subject_id = ANY(%s) AND r.predicate = 'investigates' "
        "AND r.retracted_at IS NULL ORDER BY rn.created_at",
        (goal_ids,),
    ).fetchall()
    about: dict[UUID, list[dict]] = {}
    for r in rows:
        about.setdefault(r["goal_id"], []).append(r)
    return about


def programs(conn: Connection) -> list[dict]:
    return conn.execute(
        "SELECT p.*, n.created_at, "
        "(SELECT count(*) FROM goals g WHERE g.program_id = p.id) AS goals "
        "FROM programs p JOIN nodes n ON n.id = p.id "
        "ORDER BY CASE p.status WHEN 'active' THEN 0 WHEN 'proposed' THEN 1 "
        "WHEN 'paused' THEN 2 ELSE 3 END, p.priority, n.created_at"
    ).fetchall()


def decisions_waiting(conn: Connection) -> list[dict]:
    return conn.execute(
        "SELECT d.*, a.name AS proposed_by, n.created_at FROM decisions d "
        "JOIN nodes n ON n.id = d.id JOIN actors a ON a.id = n.created_by "
        "WHERE d.status = 'proposed' ORDER BY n.created_at"
    ).fetchall()


def decisions_resolved(conn: Connection, limit: int = 20) -> list[dict]:
    """Decisions already taken, newest first, with the owner's reason for
    a rejection when one was given."""
    return conn.execute(
        "SELECT d.*, a.name AS proposed_by, n.created_at, "
        "(SELECT k.argument FROM critiques k WHERE k.target_id = d.id "
        " ORDER BY k.id LIMIT 1) AS reason "
        "FROM decisions d JOIN nodes n ON n.id = d.id JOIN actors a ON a.id = n.created_by "
        "WHERE d.status <> 'proposed' ORDER BY d.resolved_at DESC LIMIT %s",
        (limit,),
    ).fetchall()


def owner_challenges(conn: Connection, limit: int = 10) -> list[dict]:
    """The owner's critiques of hypotheses, open ones first, then the most
    recently settled, with how the organization answered them."""
    return conn.execute(
        "SELECT k.*, n.created_at, h.statement, h.id AS hypothesis_id, "
        "(SELECT l.at FROM audit_log l WHERE l.table_name = 'critiques' AND l.row_id = k.id "
        " AND l.action = 'UPDATE' ORDER BY l.at DESC LIMIT 1) AS settled_at "
        "FROM critiques k JOIN nodes n ON n.id = k.id JOIN hypotheses h ON h.id = k.target_id "
        "WHERE n.created_by = (SELECT id FROM actors WHERE name = 'owner') "
        "ORDER BY k.status = 'open' DESC, settled_at DESC NULLS LAST, n.created_at DESC LIMIT %s",
        (limit,),
    ).fetchall()


def feeds(conn: Connection) -> list[dict]:
    return conn.execute(
        "SELECT f.*, d.slug FROM approved_sources f JOIN domains d ON d.id = f.domain_id "
        "WHERE f.kind = 'feed' ORDER BY d.slug, f.status = 'retired', f.created_at"
    ).fetchall()


def questions(conn: Connection, limit: int = 30) -> list[dict]:
    return conn.execute(
        "SELECT q.*, n.created_at, (SELECT array_agg(d.slug) FROM node_domains nd "
        "JOIN domains d ON d.id = nd.domain_id WHERE nd.node_id = q.id) AS domains "
        "FROM questions q JOIN nodes n ON n.id = q.id ORDER BY n.created_at DESC LIMIT %s",
        (limit,),
    ).fetchall()


def question_detail(conn: Connection, question_id: UUID) -> dict | None:
    """A question, its answer, and the records the answer cites, each with
    where to read more: evidence points to what it bears on."""
    q = conn.execute(
        "SELECT q.*, n.created_at, r.model, r.role_version FROM questions q "
        "JOIN nodes n ON n.id = q.id LEFT JOIN runs r ON r.id = ("
        " SELECT l.run_id FROM audit_log l WHERE l.table_name = 'questions' "
        " AND l.row_id = q.id AND l.action = 'UPDATE' ORDER BY l.at DESC LIMIT 1) "
        "WHERE q.id = %s",
        (question_id,),
    ).fetchone()
    if q is None:
        return None
    cites = conn.execute(
        "SELECT r.rationale AS number, t.id, t.kind, "
        "coalesce(h.statement, o.statement, e.summary, n.name) AS text, "
        "coalesce(h.status, o.status, n.status) AS status, e.excerpt, "
        "(SELECT l.target_id FROM evidence_links l WHERE l.evidence_id = e.id "
        " AND l.retracted_at IS NULL LIMIT 1) AS bears_on, "
        "(SELECT l.target_kind FROM evidence_links l WHERE l.evidence_id = e.id "
        " AND l.retracted_at IS NULL LIMIT 1) AS bears_on_kind "
        "FROM relationships r JOIN nodes t ON t.id = r.object_id "
        "LEFT JOIN hypotheses h ON h.id = t.id LEFT JOIN observations o ON o.id = t.id "
        "LEFT JOIN evidence e ON e.id = t.id LEFT JOIN entities n ON n.id = t.id "
        "WHERE r.subject_id = %s AND r.predicate = 'cites' AND r.retracted_at IS NULL "
        "ORDER BY length(r.rationale), r.rationale",
        (question_id,),
    ).fetchall()
    return {"question": q, "cites": cites}


def missions(conn: Connection) -> dict:
    """The organization's mission and each domain's, each with its history
    (newest first; the first is current unless it was superseded)."""
    return {
        "organization": memory.missions(conn, None),
        "domains": [
            {"domain": d, "missions": memory.missions(conn, d["id"])}
            for d in domains(conn)
            if d["status"] != "retired"
        ],
    }


# ---------------------------------------------------------------------------
# Contradictions
# ---------------------------------------------------------------------------


def contradictions(conn: Connection) -> dict:
    """Where the organization's knowledge disagrees with itself: evidence
    pulling both ways, serious objections that stand or could not be
    settled, and hypotheses the Researcher believed but the review rejected."""
    return {
        "contested": conn.execute(
            "SELECT * FROM hypothesis_overview WHERE status = ANY(%s) "
            "AND supporting_evidence > 0 AND contradicting_evidence > 0 "
            "ORDER BY least(supporting_evidence, contradicting_evidence) DESC, "
            "current_confidence DESC NULLS LAST",
            (list(LIVE),),
        ).fetchall(),
        "unsettled": conn.execute(
            "SELECT k.*, n.created_at, a.name AS raised_by, h.id AS hypothesis_id, "
            "h.statement, h.status AS hypothesis_status FROM critiques k "
            "JOIN nodes n ON n.id = k.id JOIN actors a ON a.id = n.created_by "
            "JOIN hypotheses h ON h.id = k.target_id "
            "WHERE h.status = ANY(%(live)s) AND k.severity >= %(blocking)s AND ("
            " k.status = 'upheld' OR (k.status = 'open' AND EXISTS ("
            "  SELECT 1 FROM jobs j WHERE j.kind = 'resolve' AND j.status = 'succeeded' "
            "  AND j.payload->>'hypothesis_id' = h.id::text "
            "  AND (j.payload->>'round')::int >= %(rounds)s))) "
            "ORDER BY k.severity DESC, n.created_at DESC",
            {"live": list(LIVE), "blocking": BLOCKING_SEVERITY, "rounds": MAX_ROUNDS},
        ).fetchall(),
        "overruled": conn.execute(
            "SELECT h.id, h.statement, o.current_confidence, o.created_at, "
            "max(c.confidence) AS researcher_confidence FROM hypotheses h "
            "JOIN hypothesis_overview o ON o.id = h.id "
            "JOIN confidence_assessments c ON c.target_id = h.id "
            "JOIN actors a ON a.id = c.assessed_by AND a.name = 'researcher' "
            "WHERE h.status = 'rejected' GROUP BY h.id, o.current_confidence, o.created_at "
            "HAVING max(c.confidence) >= %s ORDER BY o.created_at DESC",
            (ACCEPT_AT,),
        ).fetchall(),
    }


def contradiction_count(found: dict) -> int:
    return sum(len(v) for v in found.values())


# ---------------------------------------------------------------------------
# Recent discoveries
# ---------------------------------------------------------------------------


def recent_observations(conn: Connection, since: datetime, limit: int = 50) -> list[dict]:
    return conn.execute(
        "SELECT o.id, o.statement, o.status, o.recommendation, n.created_at, "
        "a.name AS recorded_by FROM observations o JOIN nodes n ON n.id = o.id "
        "JOIN actors a ON a.id = n.created_by WHERE n.created_at >= %s "
        "ORDER BY n.created_at DESC LIMIT %s",
        (since, limit),
    ).fetchall()


def recent_status_changes(conn: Connection, since: datetime, limit: int = 50) -> list[dict]:
    """Hypotheses that were proposed, accepted, rejected or otherwise moved."""
    return conn.execute(
        "SELECT s.hypothesis_id AS id, s.at, s.from_status, s.to_status, a.name AS actor, "
        "h.statement FROM hypothesis_status_history s JOIN hypotheses h "
        "ON h.id = s.hypothesis_id JOIN actors a ON a.id = s.actor_id "
        "WHERE s.at >= %s ORDER BY s.at DESC LIMIT %s",
        (since, limit),
    ).fetchall()


def new_entities(conn: Connection, since: datetime, limit: int = 50) -> list[dict]:
    """Entities first recorded since then, most mentioned first."""
    return conn.execute(
        "SELECT e.id, e.name, e.entity_type, n.created_at, count(r.id) AS mentions "
        "FROM entities e JOIN nodes n ON n.id = e.id "
        "LEFT JOIN relationships r ON r.object_id = e.id AND r.predicate = 'mentions' "
        "AND r.retracted_at IS NULL WHERE n.created_at >= %s AND e.status = 'active' "
        "GROUP BY e.id, n.created_at ORDER BY mentions DESC, n.created_at DESC LIMIT %s",
        (since, limit),
    ).fetchall()


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def jobs(conn: Connection, limit: int = 20) -> list[dict]:
    return conn.execute(
        "SELECT j.*, r.notes FROM jobs j LEFT JOIN runs r ON r.id = j.run_id "
        "ORDER BY j.created_at DESC LIMIT %s",
        (limit,),
    ).fetchall()


def job_counts(conn: Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


def acquisitions(conn: Connection, limit: int = 20) -> list[dict]:
    return conn.execute(
        "SELECT q.*, a.name AS actor FROM acquisitions q "
        "JOIN actors a ON a.id = q.requested_by ORDER BY q.requested_at DESC LIMIT %s",
        (limit,),
    ).fetchall()


def acquisition_request(q: dict) -> str:
    return (
        q["request"].get("query")
        or q["request"].get("url")
        or ", ".join(q["request"].get("urls", []))
    )
