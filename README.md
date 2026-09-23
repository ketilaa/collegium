# Collegium

A persistent digital research organization with institutional memory.

It continuously acquires, structures, challenges, refines and preserves
knowledge over time. It behaves like a research institute, not a chatbot:
it exists between conversations, and it becomes more valuable every month
because it accumulates observations, evidence, hypotheses, decisions and
experience.

The owner acts as board chair and sponsor, setting direction. The
organization decides how the research is done.

See [VISION.md](VISION.md).

## Status

Founding. Milestone 1 (institutional memory) has a database schema; no
application code yet.

## Getting started

Requires Docker.

```sh
docker compose up -d db          # Postgres on localhost:5432
docker compose run --rm migrate  # apply migrations
```

Technical decisions and their reasons are in
[docs/decisions.md](docs/decisions.md).
