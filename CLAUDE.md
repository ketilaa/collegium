# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Milestone 1 (institutional memory) is done. Milestone 2 (research workflow) is implemented and has run end to end against Qwen2.5 7B and 14B on llama.cpp and live Tavily search, in throwaway databases. Milestone 2.5 part 1 (acquisition layer) is done. There is no API or web UI yet. The owner's secrets live in `~/.collegium/.env`: never read or print that file, only source it into a command's environment.

`VISION.md` is the source of truth for intent. `docs/decisions.md` records the technical decisions made so far and why. Read both before making design decisions, and add an entry to `docs/decisions.md` when you make a new one.

## Stack

- **Python 3.12+** for API, worker and scheduler, managed with `uv`. FastAPI for the API, psycopg 3 with plain SQL (no ORM), Pydantic for validating LLM output. Web UI server-rendered (Jinja + htmx).
- **dbmate** for migrations: plain SQL files in `db/migrations/`, each with `-- migrate:up` and an empty `-- migrate:down`. dbmate wraps each file in a transaction, so files contain no `BEGIN`/`COMMIT`. Never edit an applied migration; add a new one.
- The scheduler is a jobs table in Postgres read with `FOR UPDATE SKIP LOCKED`, not Redis or Celery.

## Commands

```sh
docker compose up -d db                  # Postgres on localhost:5432 (collegium/collegium)
docker compose run --rm migrate          # apply pending migrations with dbmate
docker compose run --rm logins           # create/update the worker and board login users
docker compose up -d                     # everything: db, migrate, logins, worker, scheduler

docker compose up -d backup              # daily pg_dump into COLLEGIUM_BACKUP_DIR (see README)

uv run pytest                            # all tests (needs the db service running)
uv run pytest tests/test_pipeline.py::test_skeptic_rejection_is_recorded   # one test
uv run ruff check . && uv run ruff format .

uv run collegium --help                  # CLI; needs env vars from .env.example
uv run collegium domain add ai-agents "AI and agents"   # owner commands use the board login
uv run collegium scout ai-agents
uv run collegium worker --drain          # process due jobs, then exit
uv run collegium hypotheses
uv run collegium why <id-prefix>          # hypothesis or observation, with its citation chain
uv run collegium jobs
uv run collegium domain sources ai-agents tavily hackernews   # Scout's discovery sources
uv run collegium acquisitions            # recent calls to external providers
uv run collegium entities [slug]         # entities mentioned most
uv run collegium resolve --all           # send hypotheses with open critiques through the loop
uv run collegium feed add ai-agents <url> # approve a feed for a domain (also list/pause/resume/retire)
```

Tests create a migrated template database and give each test a fresh copy (memory tables cannot be emptied). They connect as the `collegium` superuser with `SET ROLE collegium_worker`/`collegium_board`, so table grants are exercised, but the owner/system impersonation checks (which look at the login user) are not. Tests use `ScriptedLLM` and `FakeProvider` from `tests/conftest.py`; no model or API key is needed.

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

## Code architecture

- **Pipeline:** work is a chain of jobs in the `jobs` table: `scout` (per domain) → `research` (per observation) → `review` (per hypothesis) → `record`, plus `map` (entities mentioned, after research) and the critique loop: `record` → `resolve` (Researcher investigates open critiques) → `review` (Skeptic settles them) → `record`, at most 2 rounds. Each role enqueues the next step. Owner commands and the scheduler enqueue `scout` jobs.
- **Prepare/persist:** a role's `prepare()` (`src/collegium/roles/`) does the slow work (reading memory, searching, calling the model) outside any transaction, and returns a `persist(conn)` function. `worker.run_once` runs that function in one transaction acting as the role, together with finishing the run and the job, so knowledge and follow-up jobs commit atomically. On failure, the job is retried with backoff and nothing is written.
- **Runs:** every job execution creates a `runs` row with the model and `role_version` (a hash of the role's prompt files), and all rows written carry that run id.
- **Grounding** (`grounding.py`): the model cites search results and documents by number, not URL. Out-of-range citations are dropped, and evidence is stored only if its excerpt is found in the document. The stored excerpt is the source's own wording. Scout observations must quote their lead; names and numbers in the statement must occur in the quote (`unsupported_terms`), and a model call checks each statement against its quote. The quote is stored as evidence supporting the observation.
- **Historian:** deterministic rules, no model. It is the only role that changes a hypothesis's status (accept/reject/under review, and superseding hypotheses that an accepted one `refines`). Accepting needs confidence ≥ 0.6, supporting evidence from at least two independent sites, and no open or upheld critique of severity ≥ 3; the Skeptic's verdict is only a veto (owner's decision). Changing a rule means bumping `Historian.version()`.
- **Budget:** paid providers set `metered = True`; at most `COLLEGIUM_DAILY_CALL_BUDGET` (50) paid calls per 24 hours. Jobs stopped by the budget are deferred (`jobs.defer`) without using an attempt. Roles that make no external calls set `searches = False`.
- **Recency:** the Scout searches news within `COLLEGIUM_SCOUT_RECENT_DAYS` and drops older results. The Researcher and Skeptic search without a window.
- **Untrusted text** (`untrusted.py`): all outside text is sanitized in the acquisition layer (control tokens and hidden characters removed; injection signals stored in item metadata as `injection_signals`). Any outside text put into a prompt, including excerpts read back from memory, must be wrapped with `fence()`. Evidence from flagged documents is capped at reliability 0.3.
- **Labels in prompts:** existing hypotheses are shown to the model as `E1..En`, new ones are `H1..Hn`, and the Skeptic's target is `H`. Code maps labels to ids; the model never sees UUIDs.
- **Acquisition** (`acquisition/`): roles use the `Acquisition` facade (`discover`, `extract`, `gather`), never a provider. `Discovery` and `Extractor` are the provider protocols; vendor code lives only in adapters (`tavily.py`, `hackernews.py`). The worker gives each run a recording `Acquisition`, so every external call lands in `acquisitions` with its run, and sources carry the `acquisition_id` that found them. Providers with `thin_leads = True` get their top leads enriched from the page. `memory.py` is the only module that writes knowledge rows.
- **Prompts** live in `src/collegium/roles/prompts/`: `organization.md` is shared and each role has its own file. Changing a prompt changes that role's `role_version`.

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
- Access goes through group roles: `collegium_reader`, `collegium_worker` (agents: insert knowledge, update only lifecycle columns) and `collegium_board` (the owner's interface: also domains and resolving decisions). Only board logins may act as the `owner` actor. Any table added in a later migration must be granted to these roles explicitly. Login users (`collegium_worker_app`, `collegium_board_app`) are created by `db/logins.sql`, not by migrations, because passwords are deployment secrets.
- Actors: `owner`, `system`, one per role, and `scheduler` (kind `service`). Unaudited tables that need actor checks call `assert_session_actor()` from a trigger, as `jobs` does.
- Domains and vocabularies are data. Adding a research area never needs a migration.

## Milestones

1. Institutional memory: Postgres schema and audit history. Knowledge must survive restarts and agent replacement.
2. Research workflow: Scout, Researcher, Skeptic and Historian, with a minimal acquisition layer (`search`/`extract`, one Tavily adapter).
2.5. Knowledge acquisition layer. Part 1 done: discover/extract split, Hacker News adapter, per-domain discovery sources, call log. Part 2 so far: grounded Scout observations, crawling owner-approved feeds. Related entities (`map` jobs) and citation tracking (`collegium why`) done.
3. Strategy layer: Strategist, knowledge-gap detection and research programs. Includes the critique-resolution loop: open critiques are investigated and resolved, which is how hypotheses come to be accepted. Until then almost nothing is accepted, by design.
4. Board interface: a dashboard with Mission, Programs, Goals, Hypotheses, Contradictions, Recent Discoveries and "Ask the Organization".
5. Long-term evolution: cross-domain knowledge and belief revision.
