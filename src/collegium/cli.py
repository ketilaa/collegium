"""Command line: run the organization's processes, and act as its owner."""

import argparse
import logging
from datetime import datetime, timedelta

from collegium import jobs, memory, scheduler, worker
from collegium.acquisition import provider_from_settings
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

    p = sub.add_parser("scout", help="ask the Scout to explore a domain now (owner)")
    p.add_argument("slug")

    p = sub.add_parser("hypotheses", help="list hypotheses and current confidence")
    p.add_argument("--all", action="store_true", help="include rejected and superseded")

    p = sub.add_parser("why", help="explain why the organization holds a belief")
    p.add_argument("hypothesis", help="hypothesis id or unique prefix")

    p = sub.add_parser("jobs", help="show recent jobs")
    p.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)
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
        ),
        acquisition=provider_from_settings(settings),
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
    else:
        with _reader(settings).reading() as conn:
            for d in conn.execute("SELECT slug, name, status FROM domains ORDER BY slug"):
                print(f"{d['slug']:30} {d['status']:8} {d['name']}")


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
            "SELECT id FROM hypotheses WHERE id::text LIKE %s", (args.hypothesis + "%",)
        ).fetchall()
        if len(matches) != 1:
            raise SystemExit(f"{len(matches)} hypotheses match {args.hypothesis!r}")
        hid = matches[0]["id"]
        h = conn.execute("SELECT * FROM hypothesis_overview WHERE id = %s", (hid,)).fetchone()
        proposer = conn.execute(
            "SELECT name FROM actors WHERE id = %s", (h["proposed_by"],)
        ).fetchone()["name"]
        statuses = conn.execute(
            "SELECT s.at, s.from_status, s.to_status, a.name FROM hypothesis_status_history s "
            "JOIN actors a ON a.id = s.actor_id WHERE s.hypothesis_id = %s ORDER BY s.at",
            (hid,),
        ).fetchall()
        history = memory.confidence_history(conn, hid)
        evidence = memory.hypothesis_evidence(conn, hid)
        critiques = memory.critiques_of(conn, hid)

    print(f"{h['statement']}\n")
    print(f"Status {h['status']}, proposed by {proposer} on {_local(h['created_at']):%Y-%m-%d}")
    if h["first_accepted_at"]:
        print(f"First accepted {_local(h['first_accepted_at']):%Y-%m-%d %H:%M}")
    print("\nStatus history:")
    for s in statuses:
        print(
            f"  {_local(s['at']):%Y-%m-%d %H:%M}  {s['from_status'] or '-'} -> {s['to_status']}"
            f"  ({s['name']})"
        )
    print("\nConfidence history:")
    for c in history:
        print(
            f"  {_local(c['assessed_at']):%Y-%m-%d %H:%M}  {c['confidence']:.2f}  "
            f"({c['assessed_by']}) {c['rationale']}"
        )
    print("\nEvidence:")
    for e in evidence:
        print(f'  [{e["stance"]}] {e["summary"]}\n      "{e["excerpt"]}"\n      {e["source_uri"]}')
    print("\nCritiques:")
    for c in critiques:
        alt = (
            f"\n      Alternative: {c['alternative_explanation']}"
            if c["alternative_explanation"]
            else ""
        )
        print(f"  [{c['status']}, severity {c['severity']}] {c['argument']}{alt}")


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
    "scout": _scout,
    "hypotheses": _hypotheses,
    "why": _why,
    "jobs": _jobs,
}
