# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Milestone 1 (institutional memory) has a schema. There is no application code yet, and no build, lint or test commands. Add them here once they exist.

`VISION.md` is the source of truth for intent. `docs/decisions.md` records the technical decisions made so far and why. Read both before making design decisions, and add an entry to `docs/decisions.md` when you make a new one.

## Stack

- **Python 3.12+** for API, worker and scheduler, managed with `uv`. FastAPI for the API, psycopg 3 with plain SQL (no ORM), Pydantic for validating LLM output. Web UI server-rendered (Jinja + htmx).
- **dbmate** for migrations: plain SQL files in `db/migrations/`, each with `-- migrate:up` and an empty `-- migrate:down`. dbmate wraps each file in a transaction, so files contain no `BEGIN`/`COMMIT`. Never edit an applied migration; add a new one.
- The scheduler is a jobs table in Postgres read with `FOR UPDATE SKIP LOCKED`, not Redis or Celery.

## Commands

```sh
docker compose up -d db          # start Postgres on localhost:5432 (collegium/collegium)
docker compose run --rm migrate  # apply pending migrations with dbmate
```

Without Docker registry access, apply a migration directly: `psql -v ON_ERROR_STOP=1 --single-transaction -f db/migrations/<file>.sql`.

## What Collegium is

A persistent research organization with institutional memory, not a request/response chatbot. It keeps working between user sessions. Its core products are accumulated, structured, explainable knowledge and the history of how that knowledge changed. The user acts as board chair: they set research areas and priorities, review findings and challenge conclusions. They do not assign individual tasks.

## Architecture constraints (from VISION.md)

- **Deployment:** Docker Compose with five components: PostgreSQL, API, Worker, Scheduler and Web UI. It must run on a laptop, a small VM or a cloud VM with minimal changes.
- **PostgreSQL is the only datastore.** Do not add Kubernetes, extra databases, graph databases, event streaming or complex cloud services until a real need is shown. Model relationships as tables in Postgres.
- **Agents are roles, not services.** One worker process runs every role (Scout, Researcher, Skeptic, Strategist). Roles differ by role definition and memory, not by infrastructure.
- **One local LLM for every role** (for example Qwen3 14B or Gemma 3 12B). Do not specialize models per role.
- **Least privilege:** agents may read and search external sources, analyze them and write to organizational memory. They must not send email, post, publish, buy anything or modify any external system.
- **Provider-agnostic acquisition:** agents call abstract capabilities (`search`, `extract`, `crawl`, `discover_related`). Vendor APIs (Tavily, Exa, Firecrawl, Brave) are reached only through adapters behind those capabilities, never directly from role code. The Historian never searches externally, and the Strategist searches only when memory shows a real knowledge gap. Over time, memory should be consulted before external search.
- **Domain-agnostic:** AI is the first research domain, but adding a new domain (for example Norwegian consulting companies or energy) must not require schema or code changes.
- Prefer clarity and simplicity over scalability.

## Roles

| Role | Output | Note |
|---|---|---|
| Historian | Stored memory and audit trail | Never does research. It is the organization's identity and must survive replacement of every other role. |
| Scout | Observation proposals | Broad, breadth-first exploration; looks for weak signals. |
| Researcher | Findings, hypotheses, evidence | Investigates observations. |
| Skeptic | Critiques, confidence adjustments | Nothing enters long-term knowledge without passing the Skeptic. |
| Strategist | Goals, research programs | Finds knowledge gaps and allocates attention. |

Core workflow: Scout → Researcher → Skeptic → Historian. The Strategist sits above it and creates the investigations.

## Memory model

Memory is the most important asset. Store structured records, not free-form reports. The core concepts are Entity, Observation, Hypothesis, Evidence, Relationship, Program, Goal and Decision.

Every belief must be explainable. The schema must be able to answer:
- Why do we believe this, and since when?
- Which evidence supports it, and which contradicts it?
- Who (which role) proposed it?
- How has confidence changed over time?

This means history and provenance must be kept, not overwritten.

### Schema conventions (enforced by the database)

- Every writing transaction must first run `SELECT set_config('collegium.actor_id', '<actor uuid>', true)` and, optionally, the same for `collegium.run_id`. Writes without an actor are rejected. Provenance columns (`created_by`, `retracted_by`, `resolved_by`, ...) default to that actor; omit them, since any other value is rejected.
- Each knowledge record is a `nodes` row plus a row with the same id in its own table (`entities`, `hypotheses`, and so on). Insert both in one statement: `WITH n AS (INSERT INTO nodes (kind) VALUES ('hypothesis') RETURNING id) INSERT INTO hypotheses ...`.
- `relationships` and `critiques` are nodes too, so they can be critiqued, linked and backed by evidence. `evidence_links` and `confidence_assessments` target a node by `(target_id, target_kind)`.
- Nothing is deleted: knowledge is retired through `status`, and links are retracted with `retracted_at`. Statements are never edited; a changed claim is a new record that supersedes the old one. `confidence_assessments` and `audit_log` are append-only. Every table is audited automatically.
- Access goes through group roles: `collegium_reader`, `collegium_worker` (agents: insert knowledge, update only lifecycle columns) and `collegium_board` (the owner's interface: also domains and resolving decisions). Only board logins may act as the `owner` actor. Any table added in a later migration must be granted to these roles explicitly.
- Domains and vocabularies are data. Adding a research area never needs a migration.

## Milestones

1. Institutional memory: Postgres schema and audit history. Knowledge must survive restarts and agent replacement.
2. Research workflow: Scout, Researcher, Skeptic and Historian.
2.5. Knowledge acquisition layer: search abstraction, provider adapters, source and citation tracking.
3. Strategy layer: Strategist, knowledge-gap detection and research programs.
4. Board interface: a dashboard with Mission, Programs, Goals, Hypotheses, Contradictions, Recent Discoveries and "Ask the Organization".
5. Long-term evolution: cross-domain knowledge and belief revision.
