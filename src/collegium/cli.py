"""Command line: run the organization's processes, and act as its owner."""

import argparse
import logging
from datetime import datetime, timedelta

import truststore

from collegium import jobs, memory, scheduler, worker
from collegium.acquisition import acquisition_from_settings
from collegium.acquisition.feeds import FeedReader
from collegium.config import Settings, require
from collegium.db import Database
from collegium.llm import OpenAICompatibleLLM
from collegium.roles.base import Context


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="collegium")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("worker", help="run agent roles on queued jobs")
    p.add_argument("--drain", action="store_true", help="exit when no jobs are due")

    p = sub.add_parser("scheduler", help="enqueue recurring work")
    p.add_argument("--once", action="store_true")

    p = sub.add_parser("domain", help="manage research domains (owner)")
    dsub = p.add_subparsers(dest="action", required=True)
    a = dsub.add_parser("add")
    a.add_argument("slug")
    a.add_argument("name")
    a.add_argument("--description")
    dsub.add_parser("list")
    s = dsub.add_parser("sources", help="show or set the Scout's discovery sources")
    s.add_argument("slug")
    s.add_argument("names", nargs="*", help="e.g. tavily hackernews; 'default' to reset")

    p = sub.add_parser("feed", help="manage feeds approved for a domain (owner)")
    fsub = p.add_subparsers(dest="action", required=True)
    f = fsub.add_parser("add", help="approve a feed; it is fetched once to check it")
    f.add_argument("slug")
    f.add_argument("url")
    f.add_argument("--title")
    f = fsub.add_parser("list")
    f.add_argument("slug", nargs="?")
    for action in ("pause", "resume", "retire"):
        f = fsub.add_parser(action)
        f.add_argument("slug")
        f.add_argument("url")

    p = sub.add_parser("scout", help="ask the Scout to explore a domain now (owner)")
    p.add_argument("slug")

    p = sub.add_parser("hypotheses", help="list hypotheses and current confidence")
    p.add_argument("--all", action="store_true", help="include rejected and superseded")

    p = sub.add_parser("why", help="explain a hypothesis or observation and its sources")
    p.add_argument("id", help="hypothesis or observation id, or a unique prefix")

    p = sub.add_parser("entities", help="entities the organization knows, most mentioned first")
    p.add_argument("slug", nargs="?", help="only this domain")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("acquisitions", help="show recent calls to external providers")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("jobs", help="show recent jobs")
    p.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)
    # Verify TLS against the operating system's trust store rather than
    # Python's bundled one, so networks that inspect HTTPS with a locally
    # trusted certificate (e.g. Zscaler) work as they do in a browser.
    truststore.inject_into_ssl()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings()
    COMMANDS[args.command](args, settings)


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


def _worker(args, settings: Settings) -> None:
    db = Database(require(settings.worker_database_url, "COLLEGIUM_WORKER_DATABASE_URL"))
    ctx = Context(
        db=db,
        llm=OpenAICompatibleLLM(
            settings.llm_base_url,
            settings.llm_model,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
        ),
        acquisition=acquisition_from_settings(settings),
        settings=settings,
    )
    if args.drain:
        print(f"ran {worker.drain(ctx)} jobs")
    else:
        worker.run_forever(ctx)


def _scheduler(args, settings: Settings) -> None:
    db = Database(require(settings.worker_database_url, "COLLEGIUM_WORKER_DATABASE_URL"))
    interval = timedelta(hours=settings.scout_interval_hours)
    if args.once:
        print(f"enqueued {scheduler.tick(db, interval)} jobs")
    else:
        scheduler.run_forever(db, interval)


# ---------------------------------------------------------------------------
# Owner commands
# ---------------------------------------------------------------------------


def _local(moment: datetime) -> datetime:
    """Database timestamps in the owner's local time zone."""
    return moment.astimezone()


def _board(settings: Settings) -> Database:
    return Database(require(settings.board_database_url, "COLLEGIUM_BOARD_DATABASE_URL"))


def _reader(settings: Settings) -> Database:
    url = settings.board_database_url or settings.worker_database_url
    return Database(require(url, "COLLEGIUM_BOARD_DATABASE_URL"))


def _domain(args, settings: Settings) -> None:
    if args.action == "add":
        with _board(settings).acting_as("owner") as conn:
            conn.execute(
                "INSERT INTO domains (slug, name, description) VALUES (%s, %s, %s)",
                (args.slug, args.name, args.description),
            )
        print(f"added domain {args.slug}")
    elif args.action == "sources":
        _domain_sources(args, settings)
    else:
        with _reader(settings).reading() as conn:
            rows = conn.execute(
                "SELECT slug, name, status, discovery_sources FROM domains ORDER BY slug"
            )
            for d in rows:
                sources = ", ".join(d["discovery_sources"]) or "default"
                print(f"{d['slug']:30} {d['status']:8} {d['name']}  [{sources}]")


def _domain_sources(args, settings: Settings) -> None:
    if args.names:
        names = [] if args.names == ["default"] else args.names
        known = acquisition_from_settings(settings).sources
        unknown = sorted(set(names) - set(known))
        if unknown:
            raise SystemExit(f"unknown sources {unknown}; available: {known}")
        with _board(settings).acting_as("owner") as conn:
            updated = conn.execute(
                "UPDATE domains SET discovery_sources = %s WHERE slug = %s", (names, args.slug)
            ).rowcount
        if not updated:
            raise SystemExit(f"no domain {args.slug!r}")
    with _reader(settings).reading() as conn:
        domain = memory.domain_by_slug(conn, args.slug)
    if domain is None:
        raise SystemExit(f"no domain {args.slug!r}")
    print(f"{args.slug}: {', '.join(domain['discovery_sources']) or 'default'}")


def _entities(args, settings: Settings) -> None:
    with _reader(settings).reading() as conn:
        if args.slug:
            domain = memory.domain_by_slug(conn, args.slug)
            if domain is None:
                raise SystemExit(f"no domain {args.slug!r}")
            domain_ids = [domain["id"]]
        else:
            domain_ids = [d["id"] for d in memory.active_domains(conn)]
        rows = memory.top_entities(conn, domain_ids, limit=args.limit)
    for e in rows:
        print(f"{e['mentions']:4}  {e['entity_type']:12} {e['name']}")


def _acquisitions(args, settings: Settings) -> None:
    with _reader(settings).reading() as conn:
        rows = conn.execute(
            "SELECT q.*, a.name AS actor FROM acquisitions q "
            "JOIN actors a ON a.id = q.requested_by ORDER BY q.requested_at DESC LIMIT %s",
            (args.limit,),
        ).fetchall()
    for q in rows:
        request = q["request"].get("query") or ", ".join(q["request"].get("urls", []))
        outcome = f"error: {q['error']}" if q["error"] else f"{q['result_count']} results"
        print(
            f"{_local(q['requested_at']):%m-%d %H:%M}  {q['actor']:10} {q['capability']:8} "
            f"{q['provider']:10} {outcome:12}  {request}"
        )


FEED_STATUS = {"pause": "paused", "resume": "active", "retire": "retired"}


def _feed(args, settings: Settings) -> None:
    if args.action == "list":
        with _reader(settings).reading() as conn:
            rows = conn.execute(
                "SELECT d.slug, f.url, f.title, f.status FROM approved_sources f "
                "JOIN domains d ON d.id = f.domain_id "
                "WHERE f.kind = 'feed' AND (%s::text IS NULL OR d.slug = %s) "
                "ORDER BY d.slug, f.created_at",
                (args.slug, args.slug),
            ).fetchall()
        for f in rows:
            print(f"{f['slug']:20} {f['status']:8} {f['url']}  {f['title'] or ''}")
        return

    with _board(settings).acting_as("owner") as conn:
        domain = memory.domain_by_slug(conn, args.slug)
        if domain is None:
            raise SystemExit(f"no domain {args.slug!r}")
        if args.action == "add":
            try:
                items = FeedReader().crawl(args.url, max_items=100)
            except Exception as e:
                raise SystemExit(f"could not read {args.url} as a feed: {e}") from e
            conn.execute(
                "INSERT INTO approved_sources (domain_id, kind, url, title) "
                "VALUES (%s, 'feed', %s, %s)",
                (domain["id"], args.url, args.title),
            )
            print(f"approved feed for {args.slug} ({len(items)} items now)")
        else:
            updated = conn.execute(
                "UPDATE approved_sources SET status = %s WHERE domain_id = %s AND url = %s",
                (FEED_STATUS[args.action], domain["id"], args.url),
            ).rowcount
            if not updated:
                raise SystemExit(f"no feed {args.url} for {args.slug}")
            print(f"{args.url}: {FEED_STATUS[args.action]}")


def _scout(args, settings: Settings) -> None:
    with _board(settings).acting_as("owner") as conn:
        domain = memory.domain_by_slug(conn, args.slug)
        if domain is None:
            raise SystemExit(f"no domain {args.slug!r}")
        job_id = jobs.enqueue(conn, "scout", {"domain_id": domain["id"]}, priority=2)
    print(f"queued scout job {job_id}")


def _hypotheses(args, settings: Settings) -> None:
    where = "" if args.all else "WHERE status IN ('proposed', 'under_review', 'accepted')"
    with _reader(settings).reading() as conn:
        rows = conn.execute(
            f"SELECT * FROM hypothesis_overview {where} ORDER BY current_confidence DESC NULLS LAST"
        ).fetchall()
    for h in rows:
        conf = f"{h['current_confidence']:.2f}" if h["current_confidence"] is not None else "  - "
        print(
            f"{str(h['id'])[:8]}  {h['status']:12} {conf}  "
            f"+{h['supporting_evidence']}/-{h['contradicting_evidence']} "
            f"!{h['open_critiques']}  {h['statement']}"
        )


def _why(args, settings: Settings) -> None:
    with _reader(settings).reading() as conn:
        matches = conn.execute(
            "SELECT id, kind FROM nodes WHERE kind IN ('hypothesis', 'observation') "
            "AND id::text LIKE %s",
            (args.id + "%",),
        ).fetchall()
        if len(matches) != 1:
            raise SystemExit(f"{len(matches)} hypotheses or observations match {args.id!r}")
        node = matches[0]
        if node["kind"] == "hypothesis":
            _why_hypothesis(conn, node["id"])
        else:
            _why_observation(conn, node["id"])


def _why_hypothesis(conn, hid) -> None:
    h = conn.execute("SELECT * FROM hypothesis_overview WHERE id = %s", (hid,)).fetchone()
    proposer = conn.execute("SELECT name FROM actors WHERE id = %s", (h["proposed_by"],))
    statuses = conn.execute(
        "SELECT s.at, s.from_status, s.to_status, a.name FROM hypothesis_status_history s "
        "JOIN actors a ON a.id = s.actor_id WHERE s.hypothesis_id = %s ORDER BY s.at",
        (hid,),
    ).fetchall()
    derived = conn.execute(
        "SELECT o.id, o.statement FROM relationships r JOIN observations o ON o.id = r.object_id "
        "WHERE r.subject_id = %s AND r.predicate = 'derived_from' AND r.retracted_at IS NULL",
        (hid,),
    ).fetchall()

    print(f"Hypothesis: {h['statement']}\n")
    print(
        f"Status {h['status']}, proposed by {proposer.fetchone()['name']} "
        f"on {_local(h['created_at']):%Y-%m-%d}"
    )
    if h["first_accepted_at"]:
        print(f"First accepted {_local(h['first_accepted_at']):%Y-%m-%d %H:%M}")
    if derived:
        print("\nDerived from:")
        for o in derived:
            print(f"  {str(o['id'])[:8]}  {o['statement']}")
    print("\nStatus history:")
    for s in statuses:
        print(
            f"  {_local(s['at']):%Y-%m-%d %H:%M}  {s['from_status'] or '-'} -> {s['to_status']}"
            f"  ({s['name']})"
        )
    print("\nConfidence history:")
    for c in memory.confidence_history(conn, hid):
        print(
            f"  {_local(c['assessed_at']):%Y-%m-%d %H:%M}  {c['confidence']:.2f}  "
            f"({c['assessed_by']}) {c['rationale']}"
        )
    _print_citations(memory.citations(conn, hid))
    print("\nCritiques:")
    for c in memory.critiques_of(conn, hid):
        alt = (
            f"\n      Alternative: {c['alternative_explanation']}"
            if c["alternative_explanation"]
            else ""
        )
        print(f"  [{c['status']}, severity {c['severity']}] {c['argument']}{alt}")


def _why_observation(conn, oid) -> None:
    o = conn.execute(
        "SELECT o.*, n.created_at, a.name AS recorded_by FROM observations o "
        "JOIN nodes n ON n.id = o.id JOIN actors a ON a.id = n.created_by WHERE o.id = %s",
        (oid,),
    ).fetchone()
    hypotheses = conn.execute(
        "SELECT h.id, h.statement, h.status FROM relationships r "
        "JOIN hypotheses h ON h.id = r.subject_id "
        "WHERE r.object_id = %s AND r.predicate = 'derived_from' AND r.retracted_at IS NULL",
        (oid,),
    ).fetchall()
    entities = conn.execute(
        "SELECT e.name, e.entity_type FROM relationships r JOIN entities e ON e.id = r.object_id "
        "WHERE r.subject_id = %s AND r.predicate = 'mentions' AND r.retracted_at IS NULL "
        "ORDER BY e.name",
        (oid,),
    ).fetchall()
    print(f"Observation: {o['statement']}\n")
    when = f", occurred {_local(o['occurred_at']):%Y-%m-%d}" if o["occurred_at"] else ""
    print(
        f"Status {o['status']}, recorded by {o['recorded_by']} "
        f"on {_local(o['created_at']):%Y-%m-%d}{when}"
    )
    if o["recommendation"]:
        print(f"Why it may matter: {o['recommendation']}")
    if entities:
        print("Mentions: " + ", ".join(f"{e['name']} ({e['entity_type']})" for e in entities))
    _print_citations(memory.citations(conn, oid))
    if hypotheses:
        print("\nHypotheses derived from it:")
        for h in hypotheses:
            print(f"  {str(h['id'])[:8]}  {h['status']:12} {h['statement']}")


def _print_citations(rows: list[dict]) -> None:
    print("\nEvidence:")
    for e in rows:
        extra = [f"reliability {e['reliability']}"] if e["reliability"] is not None else []
        signals = (e["metadata"] or {}).get("injection_signals")
        if signals:
            extra.append(f"flagged: {', '.join(signals)}")
        print(f"  [{e['stance']}] {e['summary']}" + (f"  ({'; '.join(extra)})" if extra else ""))
        print(f'      "{e["excerpt"]}"')
        published = f", published {_local(e['published_at']):%Y-%m-%d}" if e["published_at"] else ""
        print(f"      Source: {e['source_title'] or ''} <{e['uri']}>{published}")
        print(f"      {_found_via(e)}")


def _found_via(e: dict) -> str:
    """How the organization came across a source."""
    if e["capability"] is None:
        return "Found: not recorded"
    request = e["request"] or {}
    if e["capability"] == "crawl":
        how = f"reading approved feed {request.get('url')}"
    else:
        how = f'searching {e["provider"]} for "{request.get("query")}"'
    return f"Found by the {e['found_by']} {how} on {_local(e['requested_at']):%Y-%m-%d %H:%M}"


def _jobs(args, settings: Settings) -> None:
    with _reader(settings).reading() as conn:
        rows = conn.execute(
            "SELECT j.*, r.notes FROM jobs j LEFT JOIN runs r ON r.id = j.run_id "
            "ORDER BY j.created_at DESC LIMIT %s",
            (args.limit,),
        ).fetchall()
    for j in rows:
        detail = j["last_error"] if j["status"] != "succeeded" else j["notes"]
        print(
            f"{_local(j['created_at']):%m-%d %H:%M}  {j['kind']:8} {j['status']:9} "
            f"x{j['attempts']}  {detail or ''}"
        )


COMMANDS = {
    "worker": _worker,
    "scheduler": _scheduler,
    "domain": _domain,
    "feed": _feed,
    "scout": _scout,
    "hypotheses": _hypotheses,
    "why": _why,
    "jobs": _jobs,
    "acquisitions": _acquisitions,
    "entities": _entities,
}
