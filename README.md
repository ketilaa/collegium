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

Running daily on a laptop with a local 14B model. Done: institutional
memory, the Scout → Researcher → Skeptic → Historian workflow with critique
resolution, the Strategist, a board for the owner (missions, goals,
hypotheses, contradictions, discoveries, decisions, Ask the Organization),
free-first search and reading, and a community agent on Moltbook whose
every post the owner approves.

## Getting started

Requires Docker, [uv](https://docs.astral.sh/uv/) and a local model server
with an OpenAI-compatible API (e.g. [Ollama](https://ollama.com) or
llama.cpp's `llama-server` with a 14B model). Search runs on the bundled
SearXNG; a [Tavily](https://tavily.com) API key is an optional paid fallback.

```sh
cp .env.example .env                # set COLLEGIUM_CONTACT; optionally TAVILY_API_KEY
docker compose up -d                # database, migrations, worker, scheduler, board, search
set -a; . ./.env; set +a            # for the CLI on the host

uv run collegium domain add ai-agents "AI and agents"
uv run collegium scout ai-agents    # or wait for the scheduler
uv run collegium jobs               # follow the work
uv run collegium hypotheses         # what the organization believes
uv run collegium why <id>           # and why
```

The board is at <http://localhost:8000> once `docker compose up -d` is
running (or `uv run collegium web` on the host). It has no login yet and
listens on localhost only.

Run the tests with `uv run pytest` (needs `docker compose up -d db`).

## Behind a TLS-inspecting proxy (Zscaler and the like)

If outside calls fail with `CERTIFICATE_VERIFY_FAILED`, the network is
re-signing HTTPS with its own root certificate. Copy
`compose.override.example.yaml` to `compose.override.yaml` and build the
certificate bundle it describes; Compose then mounts it into the containers.
With Colima, image pulls also need the root in the VM (a `provision` step
in `~/.colima/default/colima.yaml`).

## Backups

The `backup` service dumps the database every 24 hours into
`COLLEGIUM_BACKUP_DIR` (default `./backups`), checks each dump is readable,
and prunes backups older than 30 days. Keep the directory somewhere that is
itself backed up, outside the repository.

```sh
docker compose up -d backup                          # scheduled backups
docker compose run --rm backup sh /db/backup.sh      # one now
docker compose run --rm backup sh /db/restore.sh /backups/collegium-<stamp>.dump
                                                     # restore into collegium_restored
```

A restore never overwrites the live database; check the restored copy, then
switch by renaming databases.

Technical decisions and their reasons are in
[docs/decisions.md](docs/decisions.md).

## License

[Apache License 2.0](LICENSE).
