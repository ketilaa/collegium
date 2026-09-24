"""Command line: run the organization's processes, and act as its owner."""

import argparse
import logging
from datetime import datetime

import truststore

from collegium import board, jobs, memory, owner, scheduler, worker
from collegium.acquisition import acquisition_from_settings, discovery_source_names
from collegium.acquisition.feeds import FeedReader
from collegium.config import Settings, require
from collegium.db import Database
from collegium.llm import OpenAICompatibleLLM
from collegium.roles.base import Context
from collegium.roles.historian import BLOCKING_SEVERITY


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="collegium")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("worker", help="run agent roles on queued jobs")
    p.add_argument("--drain", action="store_true", help="exit when no jobs are due")

    p = sub.add_parser("scheduler", help="enqueue recurring work")
    p.add_argument("--once", action="store_true")

    p = sub.add_parser("web", help="serve the board (no login yet: keep it on localhost)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser("domain", help="manage research domains (owner)")
    dsub = p.add_subparsers(dest="action", required=True)
    a = dsub.add_parser("add")
    a.add_argument("slug")
    a.add_argument("name")
    a.add_argument("--description")
    dsub.add_parser("list")
    for action in ("pause", "resume", "retire"):
        d = dsub.add_parser(action)
        d.add_argument("slug")
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

    p = sub.add_parser("strategize", help="ask the Strategist to plan a domain now (owner)")
    p.add_argument("slug")

    p = sub.add_parser("goals", help="the organization's goals")
    p.add_argument("--all", action="store_true", help="include achieved and abandoned")

    p = sub.add_parser("mission", help="show the missions, or set one (owner)")
    p.add_argument("statement", nargs="?", help="the new mission; omit to show")
    p.add_argument("--domain", help="a domain's mission instead of the organization's")

    sub.add_parser("programs", help="research programs, proposed and open")

    sub.add_parser("decisions", help="decisions waiting for the owner")

    p = sub.add_parser("approve", help="approve a proposed decision (owner)")
    p.add_argument("decision", help="decision id or unique prefix")
    p = sub.add_parser("reject", help="reject a proposed decision (owner)")
    p.add_argument("decision", help="decision id or unique prefix")
    p.add_argument("--reason", help="kept as your upheld objection to the proposal")

    p = sub.add_parser("challenge", help="critique a hypothesis; it goes through the loop (owner)")
    p.add_argument("hypothesis", help="id or unique prefix")
    p.add_argument("argument", help="what you object to")
    p.add_argument("--alternative", help="an alternative explanation")
    p.add_argument("--severity", type=int, default=3, choices=range(1, 6))

    p = sub.add_parser("resolve", help="send hypotheses with open critiques to be resolved (owner)")
    p.add_argument("hypothesis", nargs="?", help="id or prefix; omit with --all")
    p.add_argument(
        "--all", action="store_true", help="every live hypothesis with blocking critiques"
    )

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
    try:
        COMMANDS[args.command](args, settings)
    except owner.OwnerError as e:
        raise SystemExit(str(e)) from e


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
        # A manual run: the owner is watching, whatever the hour.
        print(f"ran {worker.drain(ctx)} jobs")
    else:
        worker.run_forever(ctx, settings.working_hours())


def _scheduler(args, settings: Settings) -> None:
    db = Database(require(settings.worker_database_url, "COLLEGIUM_WORKER_DATABASE_URL"))
    hours = settings.working_hours()
    if args.once:
        if not hours.is_open():
            print(f"outside working hours ({hours.describe()}); nothing queued")
        else:
            print(f"enqueued {scheduler.tick(db, hours)} jobs")
    else:
        scheduler.run_forever(db, hours)


def _web(args, settings: Settings) -> None:
    import uvicorn

    from collegium.web import create_app

    url = require(settings.board_database_url, "COLLEGIUM_BOARD_DATABASE_URL")
    uvicorn.run(create_app(settings, lambda: Database(url)), host=args.host, port=args.port)


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


DOMAIN_STATUS = {"pause": "paused", "resume": "active", "retire": "retired"}


def _domain(args, settings: Settings) -> None:
    if args.action == "add":
        with _board(settings).acting_as("owner") as conn:
            owner.add_domain(conn, args.slug, args.name, args.description)
        print(f"added domain {args.slug}")
    elif args.action == "sources":
        _domain_sources(args, settings)
    elif args.action in DOMAIN_STATUS:
        with _board(settings).acting_as("owner") as conn:
            owner.set_domain_status(conn, args.slug, DOMAIN_STATUS[args.action])
        print(f"{args.slug}: {DOMAIN_STATUS[args.action]}")
    else:
        with _reader(settings).reading() as conn:
            for d in board.domains(conn):
                sources = ", ".join(d["discovery_sources"]) or "default"
                print(f"{d['slug']:30} {d['status']:8} {d['name']}  [{sources}]")


def _domain_sources(args, settings: Settings) -> None:
    if args.names:
        names = [] if args.names == ["default"] else args.names
        with _board(settings).acting_as("owner") as conn:
            owner.set_discovery_sources(conn, args.slug, names, discovery_source_names(settings))
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
        rows = board.acquisitions(conn, args.limit)
    for q in rows:
        request = board.acquisition_request(q)
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
        if args.action == "add":
            count = owner.approve_feed(conn, args.slug, args.url, args.title, FeedReader())
            print(f"approved feed for {args.slug} ({count} items now)")
        else:
            owner.set_feed_status(conn, args.slug, args.url, FEED_STATUS[args.action])
            print(f"{args.url}: {FEED_STATUS[args.action]}")


def _scout(args, settings: Settings) -> None:
    with _board(settings).acting_as("owner") as conn:
        job_id = owner.request_work(conn, "scout", args.slug)
    print(f"queued scout job {job_id}")


def _strategize(args, settings: Settings) -> None:
    with _board(settings).acting_as("owner") as conn:
        job_id = owner.request_work(conn, "strategize", args.slug)
    print(f"queued planning job {job_id}")


def _goals(args, settings: Settings) -> None:
    with _reader(settings).reading() as conn:
        rows = board.goals(conn, include_all=args.all)
    for g in rows:
        print(
            f"{str(g['id'])[:8]}  {g['status']:9} p{g['priority']}  {g['jobs']:2} jobs  "
            f"{g['statement']}\n{'':24}done when: {g['success_criteria']}"
        )


def _mission(args, settings: Settings) -> None:
    if args.statement:
        with _board(settings).acting_as("owner") as conn:
            owner.set_mission(conn, args.statement, args.domain)
        print("mission set")
        return
    with _reader(settings).reading() as conn:
        missions = board.missions(conn)
    current = [m for m in missions["organization"] if m["status"] == "approved"]
    print(f"Organization: {current[0]['statement'] if current else '(not set)'}")
    for d in missions["domains"]:
        current = [m for m in d["missions"] if m["status"] == "approved"]
        print(f"{d['domain']['slug']}: {current[0]['statement'] if current else '(not set)'}")


def _programs(args, settings: Settings) -> None:
    with _reader(settings).reading() as conn:
        rows = board.programs(conn)
    for p in rows:
        print(f"{str(p['id'])[:8]}  {p['status']:9} {p['name']}\n{'':20}{p['charter']}")


def _decisions(args, settings: Settings) -> None:
    with _reader(settings).reading() as conn:
        rows = board.decisions_waiting(conn)
    for d in rows:
        print(
            f"{str(d['id'])[:8]}  proposed by the {d['proposed_by']} on "
            f"{_local(d['created_at']):%Y-%m-%d}\n  {d['statement']}\n  Why: {d['rationale']}"
        )
    if not rows:
        print("no decisions waiting")


def _resolve_decision(args, settings: Settings, status: str) -> None:
    with _board(settings).acting_as("owner") as conn:
        rows = conn.execute(
            "SELECT id FROM decisions WHERE status = 'proposed' AND id::text LIKE %s",
            (args.decision + "%",),
        ).fetchall()
        if len(rows) != 1:
            raise SystemExit(f"{len(rows)} proposed decisions match {args.decision!r}")
        decision_id = rows[0]["id"]
        program = owner.resolve_decision(conn, decision_id, status, getattr(args, "reason", None))
    program_status = "active" if status == "approved" else "closed"
    what = f"; program {program} is now {program_status}" if program else ""
    print(f"decision {str(decision_id)[:8]} {status}{what}")


def _challenge(args, settings: Settings) -> None:
    with _board(settings).acting_as("owner") as conn:
        matches = board.match_nodes(conn, args.hypothesis, ["hypothesis"])
        if len(matches) != 1:
            raise SystemExit(f"{len(matches)} hypotheses match {args.hypothesis!r}")
        owner.challenge(
            conn,
            matches[0]["id"],
            args.argument,
            alternative=args.alternative,
            severity=args.severity,
        )
    print("critique recorded; the Researcher will investigate it and the Skeptic settle it")


def _resolve(args, settings: Settings) -> None:
    if not args.all and not args.hypothesis:
        raise SystemExit("give a hypothesis or --all")
    with _board(settings).acting_as("owner") as conn:
        if args.all:
            rows = conn.execute(
                "SELECT DISTINCT h.id FROM hypotheses h JOIN critiques k ON k.target_id = h.id "
                "WHERE h.status = ANY(%s) AND k.status = 'open' AND k.severity >= %s",
                (list(memory.LIVE_HYPOTHESIS_STATUSES), BLOCKING_SEVERITY),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM hypotheses WHERE id::text LIKE %s", (args.hypothesis + "%",)
            ).fetchall()
            if len(rows) != 1:
                raise SystemExit(f"{len(rows)} hypotheses match {args.hypothesis!r}")
        for r in rows:
            jobs.enqueue(conn, "resolve", {"hypothesis_id": r["id"], "round": 1}, priority=2)
    print(f"queued critique resolution for {len(rows)} hypotheses")


def _hypotheses(args, settings: Settings) -> None:
    with _reader(settings).reading() as conn:
        rows = board.hypotheses(conn, include_all=args.all)
    for h in rows:
        conf = f"{h['current_confidence']:.2f}" if h["current_confidence"] is not None else "  - "
        print(
            f"{str(h['id'])[:8]}  {h['status']:12} {conf}  "
            f"+{h['supporting_evidence']}/-{h['contradicting_evidence']} "
            f"!{h['open_critiques']}  {h['statement']}"
        )


def _why(args, settings: Settings) -> None:
    with _reader(settings).reading() as conn:
        matches = board.match_nodes(conn, args.id, ["hypothesis", "observation"])
        if len(matches) != 1:
            raise SystemExit(f"{len(matches)} hypotheses or observations match {args.id!r}")
        node = matches[0]
        if node["kind"] == "hypothesis":
            _why_hypothesis(conn, node["id"])
        else:
            _why_observation(conn, node["id"])


def _why_hypothesis(conn, hid) -> None:
    detail = board.hypothesis_detail(conn, hid)
    h = detail["hypothesis"]
    print(f"Hypothesis: {h['statement']}\n")
    print(
        f"Status {h['status']}, proposed by {h['proposer']} on {_local(h['created_at']):%Y-%m-%d}"
    )
    if h["first_accepted_at"]:
        print(f"First accepted {_local(h['first_accepted_at']):%Y-%m-%d %H:%M}")
    if detail["derived_from"]:
        print("\nDerived from:")
        for o in detail["derived_from"]:
            print(f"  {str(o['id'])[:8]}  {o['statement']}")
    print("\nStatus history:")
    for s in detail["statuses"]:
        print(
            f"  {_local(s['at']):%Y-%m-%d %H:%M}  {s['from_status'] or '-'} -> {s['to_status']}"
            f"  ({s['name']})"
        )
    print("\nConfidence history:")
    for c in detail["confidence"]:
        print(
            f"  {_local(c['assessed_at']):%Y-%m-%d %H:%M}  {c['confidence']:.2f}  "
            f"({c['assessed_by']}) {c['rationale']}"
        )
    _print_citations(detail["citations"])
    print("\nCritiques:")
    for c in detail["critiques"]:
        alt = (
            f"\n      Alternative: {c['alternative_explanation']}"
            if c["alternative_explanation"]
            else ""
        )
        print(f"  [{c['status']}, severity {c['severity']}] {c['argument']}{alt}")
        if c["resolution"]:
            print(f"      Resolution: {c['resolution']}")


def _why_observation(conn, oid) -> None:
    detail = board.observation_detail(conn, oid)
    o = detail["observation"]
    print(f"Observation: {o['statement']}\n")
    when = f", occurred {_local(o['occurred_at']):%Y-%m-%d}" if o["occurred_at"] else ""
    print(
        f"Status {o['status']}, recorded by {o['recorded_by']} "
        f"on {_local(o['created_at']):%Y-%m-%d}{when}"
    )
    if o["recommendation"]:
        print(f"Why it may matter: {o['recommendation']}")
    if detail["entities"]:
        print(
            "Mentions: "
            + ", ".join(f"{e['name']} ({e['entity_type']})" for e in detail["entities"])
        )
    _print_citations(detail["citations"])
    if detail["hypotheses"]:
        print("\nHypotheses derived from it:")
        for h in detail["hypotheses"]:
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
        print(f"      {board.found_via(e, _local)}")


def _jobs(args, settings: Settings) -> None:
    with _reader(settings).reading() as conn:
        rows = board.jobs(conn, args.limit)
    for j in rows:
        detail = j["last_error"] if j["status"] != "succeeded" else j["notes"]
        print(
            f"{_local(j['created_at']):%m-%d %H:%M}  {j['kind']:8} {j['status']:9} "
            f"x{j['attempts']}  {detail or ''}"
        )


COMMANDS = {
    "worker": _worker,
    "scheduler": _scheduler,
    "web": _web,
    "domain": _domain,
    "feed": _feed,
    "scout": _scout,
    "hypotheses": _hypotheses,
    "resolve": _resolve,
    "challenge": _challenge,
    "strategize": _strategize,
    "goals": _goals,
    "programs": _programs,
    "mission": _mission,
    "decisions": _decisions,
    "approve": lambda args, settings: _resolve_decision(args, settings, "approved"),
    "reject": lambda args, settings: _resolve_decision(args, settings, "rejected"),
    "why": _why,
    "jobs": _jobs,
    "acquisitions": _acquisitions,
    "entities": _entities,
}
