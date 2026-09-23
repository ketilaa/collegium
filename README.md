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

Founding. Milestone 1 (institutional memory) is done. Milestone 2 (the
Scout → Researcher → Skeptic → Historian workflow) is implemented and has
run end to end against a local 7B model and live search.

## Getting started

Requires Docker, [uv](https://docs.astral.sh/uv/), a local model server with
an OpenAI-compatible API (e.g. [Ollama](https://ollama.com) with
`qwen3:14b`), and a [Tavily](https://tavily.com) API key.

```sh
cp .env.example .env                # fill in TAVILY_API_KEY
docker compose up -d                # database, migrations, worker, scheduler
set -a; . ./.env; set +a            # for the CLI on the host

uv run collegium domain add ai-agents "AI and agents"
uv run collegium scout ai-agents    # or wait for the scheduler
uv run collegium jobs               # follow the work
uv run collegium hypotheses         # what the organization believes
uv run collegium why <id>           # and why
```

Run the tests with `uv run pytest` (needs `docker compose up -d db`).

Technical decisions and their reasons are in
[docs/decisions.md](docs/decisions.md).
