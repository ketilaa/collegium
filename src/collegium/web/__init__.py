"""The board: the owner's web interface to the organization.

Server-rendered pages (Jinja, with htmx for live parts) over the same
queries the CLI uses. It connects with the board login. There is no
authentication yet, so it must only listen where the owner alone can reach
it (see docs/decisions.md).
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from collegium import board, memory
from collegium.acquisition import discovery_source_names, metered_provider_names
from collegium.config import Settings
from collegium.db import Connection, Database
from collegium.web.actions import NOTICES, add_actions
from collegium.web.charts import confidence_chart

HERE = Path(__file__).parent

# Everything shown is rendered here, and outside text is escaped, so the
# page needs nothing from anywhere else. This also keeps an injected
# excerpt from loading or running anything.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

DISCOVERY_WINDOWS = {1: "Last day", 7: "Last week", 30: "Last month"}


def create_app(settings: Settings, database: Callable[[], Database], crawler=None) -> FastAPI:
    """`database` opens a connection for one request; the board login in
    production, a role-switched test connection in tests. `crawler` reads a
    feed once when the owner approves it (a FeedReader unless given)."""
    app = FastAPI(title="Collegium", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = _templates(ZoneInfo(settings.timezone))
    hours = settings.working_hours()
    paid_providers = metered_provider_names(settings)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # Any page the owner visits could post a form here. Without a login
        # to tell the owner apart, a change is accepted only when the
        # browser says it came from the board itself.
        if request.method not in ("GET", "HEAD") and not _same_origin(request):
            response = PlainTextResponse("Cross-site request refused.", status_code=403)
        else:
            response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    # Added last, so it runs first: a request for any other host name (a
    # rebound DNS name, say) is refused before anything else.
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[h.strip() for h in settings.web_allowed_hosts.split(",") if h.strip()],
    )

    def reading() -> Iterator[Connection]:
        db = database()
        try:
            with db.reading() as conn:
                yield conn
        finally:
            db.close()

    Reading = Annotated[Connection, Depends(reading)]
    add_actions(app, templates, settings, database, crawler)

    def page(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context)

    def operations(conn: Connection) -> dict:
        used, oldest = memory.paid_calls_in_window(conn, paid_providers)
        return {
            "open": hours.is_open(),
            "hours": hours.describe(),
            "next_opening": hours.next_opening(),
            "budget": settings.daily_call_budget,
            "budget_used": used,
            "budget_frees": oldest + timedelta(hours=24) if oldest else None,
            "job_counts": board.job_counts(conn),
        }

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, conn: Reading):
        week = datetime.now(UTC) - timedelta(days=7)
        live = board.hypotheses(conn)
        return page(
            request,
            "overview.html",
            decisions=board.decisions_waiting(conn),
            challenges=board.owner_challenges(conn, limit=5),
            goals=board.goals(conn)[:5],
            programs=[p for p in board.programs(conn) if p["status"] == "active"],
            counts={s: sum(1 for h in live if h["status"] == s) for s in board.LIVE},
            changes=board.recent_status_changes(conn, week, limit=8),
            observations=board.recent_observations(conn, week, limit=5),
            ops=operations(conn),
        )

    @app.get("/programs", response_class=HTMLResponse)
    def programs(request: Request, conn: Reading):
        goals = board.goals(conn, include_all=True)
        return page(
            request,
            "programs.html",
            programs=board.programs(conn),
            goals_by_program=_group(goals, "program_id"),
        )

    @app.get("/decisions", response_class=HTMLResponse)
    def decisions(request: Request, conn: Reading):
        return page(
            request,
            "decisions.html",
            waiting=board.decisions_waiting(conn),
            resolved=board.decisions_resolved(conn),
            challenges=board.owner_challenges(conn, limit=30),
        )

    @app.get("/domains", response_class=HTMLResponse)
    def domains(request: Request, conn: Reading):
        return page(
            request,
            "domains.html",
            domains=board.domains(conn),
            feeds=_group(board.feeds(conn), "slug"),
            sources=discovery_source_names(settings),
        )

    @app.get("/goals", response_class=HTMLResponse)
    def goals(request: Request, conn: Reading, all: bool = False):
        return page(request, "goals.html", goals=board.goals(conn, include_all=all), all=all)

    @app.get("/hypotheses", response_class=HTMLResponse)
    def hypotheses(request: Request, conn: Reading, all: bool = False):
        return page(
            request,
            "hypotheses.html",
            hypotheses=board.hypotheses(conn, include_all=all),
            all=all,
        )

    @app.get("/hypotheses/{hid}", response_class=HTMLResponse)
    def hypothesis(request: Request, hid: UUID, conn: Reading):
        detail = board.hypothesis_detail(conn, hid)
        if detail is None:
            raise HTTPException(404, "No such hypothesis")
        return page(
            request,
            "hypothesis.html",
            **detail,
            chart=confidence_chart(detail["confidence"]),
            evidence=_group(detail["citations"], "stance"),
            live=detail["hypothesis"]["status"] in board.LIVE,
        )

    @app.get("/observations/{oid}", response_class=HTMLResponse)
    def observation(request: Request, oid: UUID, conn: Reading):
        detail = board.observation_detail(conn, oid)
        if detail is None:
            raise HTTPException(404, "No such observation")
        return page(
            request, "observation.html", **detail, evidence=_group(detail["citations"], "stance")
        )

    @app.get("/discoveries", response_class=HTMLResponse)
    def discoveries(request: Request, conn: Reading, days: int = 7):
        if days not in DISCOVERY_WINDOWS:
            days = 7
        since = datetime.now(UTC) - timedelta(days=days)
        return page(
            request,
            "discoveries.html",
            days=days,
            windows=DISCOVERY_WINDOWS,
            observations=board.recent_observations(conn, since),
            changes=board.recent_status_changes(conn, since),
            entities=board.new_entities(conn, since),
        )

    @app.get("/operations", response_class=HTMLResponse)
    def operations_page(request: Request, conn: Reading):
        return page(request, "operations.html", **_operations_context(conn))

    @app.get("/operations/live", response_class=HTMLResponse)
    def operations_live(request: Request, conn: Reading):
        """The operations page's contents, refreshed in place by htmx."""
        return page(request, "_operations.html", **_operations_context(conn))

    def _operations_context(conn: Connection) -> dict:
        return {
            "ops": operations(conn),
            "jobs": board.jobs(conn, 30),
            "acquisitions": board.acquisitions(conn, 20),
        }

    return app


def _same_origin(request: Request) -> bool:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site == "same-origin"
    origin = request.headers.get("origin")
    return origin is not None and origin == f"{request.url.scheme}://{request.url.netloc}"


def _group(rows: list[dict], key: str) -> dict:
    grouped: dict = {}
    for r in rows:
        grouped.setdefault(r[key], []).append(r)
    return grouped


def _templates(tz: ZoneInfo) -> Jinja2Templates:
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env

    def local(moment: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        return moment.astimezone(tz).strftime(fmt) if moment else ""

    env.filters["local"] = local
    env.filters["label"] = lambda value: (value or "").replace("_", " ")
    env.filters["short"] = lambda value: str(value)[:8]
    env.filters["confidence"] = lambda c: "–" if c is None else f"{c:.2f}"
    env.filters["http_url"] = http_url
    env.filters["found_via"] = lambda e: board.found_via(e, lambda m: m.astimezone(tz))
    env.filters["request"] = board.acquisition_request
    env.globals["TIMEZONE"] = tz.key
    # Messages after an action are named in the address, never written
    # there, so a link cannot put words on the board.
    env.globals["notice_for"] = lambda request: NOTICES.get(request.query_params.get("done", ""))
    return templates


def http_url(uri: str | None) -> str | None:
    """A source's address, only if it is safe to link to. Addresses come
    from outside, and a javascript: or data: link must never be clickable."""
    if not uri:
        return None
    return uri if urlsplit(uri).scheme in ("http", "https") else None
