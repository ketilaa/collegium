"""The owner's actions on the board: plain HTML forms that post here, act
as the owner in one transaction, and redirect back to the page they came
from."""

from collections.abc import Callable, Iterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from collegium import owner
from collegium.acquisition import discovery_source_names
from collegium.acquisition.feeds import FeedReader
from collegium.config import Settings
from collegium.db import Connection, Database

# What the board says after an action. The redirect names one of these.
NOTICES = {
    "asked": "Question received. The Researcher answers from memory, at any hour.",
    "mission": "Mission set. The Strategist plans by it from its next run.",
    "approved": "Decision approved.",
    "rejected": "Decision rejected.",
    "challenged": (
        "Your critique is recorded. The Researcher will look for evidence that settles it, "
        "and the Skeptic will decide whether it stands."
    ),
    "domain-added": "Domain added. The Strategist will plan it at the next working-day start.",
    "domain-updated": "Domain updated.",
    "sources-updated": "Discovery sources updated.",
    "feed-approved": "Feed approved. The Scout reads it on its next run.",
    "feed-updated": "Feed updated.",
    "scout": "Scout queued. It starts within working hours.",
    "strategize": "Planning queued. It starts within working hours.",
}


def add_actions(
    app: FastAPI,
    templates: Jinja2Templates,
    settings: Settings,
    database: Callable[[], Database],
    crawler=None,
) -> None:
    crawler = crawler or FeedReader()

    def acting() -> Iterator[Connection]:
        db = database()
        try:
            with db.acting_as("owner") as conn:
                yield conn
        finally:
            db.close()

    Acting = Annotated[Connection, Depends(acting)]

    @app.exception_handler(owner.OwnerError)
    def refused(request: Request, error: owner.OwnerError):
        # Raised inside the transaction, so nothing was written.
        return templates.TemplateResponse(
            request,
            "refused.html",
            {"message": str(error), "back": request.headers.get("referer")},
            status_code=400,
        )

    def done(path: str, notice: str) -> RedirectResponse:
        return RedirectResponse(f"{path}?done={notice}", status_code=303)

    @app.post("/ask")
    def ask(
        conn: Acting,
        question: Annotated[str, Form()] = "",
        domain: Annotated[str, Form()] = "",
    ):
        question_id = owner.ask(conn, question, domain or None)
        return done(f"/questions/{question_id}", "asked")

    @app.post("/mission")
    def set_mission(
        conn: Acting,
        statement: Annotated[str, Form()] = "",
        domain: Annotated[str, Form()] = "",
    ):
        owner.set_mission(conn, statement, domain or None)
        return done("/mission", "mission")

    @app.post("/decisions/{decision_id}/approve")
    def approve(decision_id: UUID, conn: Acting):
        owner.resolve_decision(conn, decision_id, "approved")
        return done("/decisions", "approved")

    @app.post("/decisions/{decision_id}/reject")
    def reject(decision_id: UUID, conn: Acting, reason: Annotated[str, Form()] = ""):
        owner.resolve_decision(conn, decision_id, "rejected", reason)
        return done("/decisions", "rejected")

    @app.post("/hypotheses/{hypothesis_id}/challenge")
    def challenge(
        hypothesis_id: UUID,
        conn: Acting,
        argument: Annotated[str, Form()] = "",
        alternative: Annotated[str, Form()] = "",
        severity: Annotated[int, Form()] = 3,
    ):
        owner.challenge(conn, hypothesis_id, argument, alternative=alternative, severity=severity)
        return done(f"/hypotheses/{hypothesis_id}", "challenged")

    @app.post("/domains")
    def add_domain(
        conn: Acting,
        slug: Annotated[str, Form()] = "",
        name: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
    ):
        owner.add_domain(conn, slug, name, description)
        return done("/domains", "domain-added")

    @app.post("/domains/{slug}/status")
    def domain_status(slug: str, conn: Acting, status: Annotated[str, Form()]):
        owner.set_domain_status(conn, slug, status)
        return done("/domains", "domain-updated")

    @app.post("/domains/{slug}/sources")
    def domain_sources(slug: str, conn: Acting, source: Annotated[list[str], Form()] = []):  # noqa: B006
        # No box ticked means the default source.
        owner.set_discovery_sources(conn, slug, source, discovery_source_names(settings))
        return done("/domains", "sources-updated")

    @app.post("/domains/{slug}/feeds")
    def approve_feed(
        slug: str,
        conn: Acting,
        url: Annotated[str, Form()] = "",
        title: Annotated[str, Form()] = "",
    ):
        owner.approve_feed(conn, slug, url, title, crawler)
        return done("/domains", "feed-approved")

    @app.post("/domains/{slug}/feeds/status")
    def feed_status(
        slug: str, conn: Acting, url: Annotated[str, Form()], status: Annotated[str, Form()]
    ):
        owner.set_feed_status(conn, slug, url, status)
        return done("/domains", "feed-updated")

    @app.post("/domains/{slug}/{kind}")
    def request_work(slug: str, kind: str, conn: Acting):
        owner.request_work(conn, kind, slug)
        return done("/domains", kind)
