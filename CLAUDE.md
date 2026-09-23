# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

No code exists yet. The repository contains only `README.md` and `VISION.md`. There are no build, lint or test commands. Add them here once the first components exist.

`VISION.md` is the source of truth for intent. Read it before making design decisions.

## What Collegium is

A persistent research organization with institutional memory, not a request/response chatbot. It keeps working between user sessions. Its core products are accumulated, structured, explainable knowledge and the history of how that knowledge changed. The user acts as board chair: they set research areas and priorities, review findings and challenge conclusions. They do not assign individual tasks.

## Architecture constraints (from VISION.md)

- **Deployment:** Docker Compose with five components: PostgreSQL, API, Worker, Scheduler and Web UI. It must run on a laptop, a small VM or a cloud VM with minimal changes.
- **PostgreSQL is the only datastore.** Do not add Kubernetes, extra databases, graph databases, event streaming or complex cloud services until a real need is shown. Model relationships as tables in Postgres.
- **Agents are roles, not services.** One worker process runs every role (Scout, Researcher, Skeptic, Strategist). Roles differ by role definition and memory, not by infrastructure.
- **One local LLM for every role** (for example Qwen3 14B or Gemma 3 12B). Do not specialize models per role.
- **Least privilege:** agents may read and search external sources, analyze them and write to organizational memory. They must not send email, post, publish, buy anything or modify any external system.
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

## Milestones

1. Institutional memory: Postgres schema and audit history. Knowledge must survive restarts and agent replacement.
2. Research workflow: Scout, Researcher, Skeptic and Historian.
3. Strategy layer: Strategist, knowledge-gap detection and research programs.
4. Board interface: a dashboard with Mission, Programs, Goals, Hypotheses, Contradictions, Recent Discoveries and "Ask the Organization".
5. Long-term evolution: cross-domain knowledge and belief revision.
