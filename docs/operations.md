# Operations: the owner's running instance

How the live organization on the owner's laptop is run, checked and
changed. For what the code does, see CLAUDE.md; for why, docs/decisions.md.

## The machine

- **Docker runs in Colima**, not Docker Desktop. Docker-level fixes go
  through the Colima VM (`colima ssh`, `colima restart`, provisioning in
  `~/.colima/default/colima.yaml`).
- **The network re-signs HTTPS (a TLS-inspecting proxy).** The Colima VM
  trusts the proxy's root (a provision step), and the git-ignored
  `compose.override.yaml` mounts `~/.collegium/ca-bundle.pem` into the
  services (see `compose.override.example.yaml`). On the host, Python tools
  that fail with certificate errors need
  `SSL_CERT_FILE=$HOME/.collegium/ca-bundle.pem`.
- **The model** runs on the host outside Docker: llama.cpp's `llama-server`
  with `Qwen/Qwen2.5-14B-Instruct-GGUF:Q4_K_M`, at `http://localhost:8080/v1`
  (`host.docker.internal:8080` from the containers).
- **Secrets** are in `~/.collegium/.env`: never read or print
  it; only pass it to commands. Every Compose command uses it:

  ```sh
  E="--env-file $HOME/.collegium/.env"
  docker compose $E ps
  ```

  It holds, among others, the Tavily and Moltbook keys, the database
  passwords, the LLM settings and `COLLEGIUM_CONTACT`. If something fails
  because of a value in it, describe the symptom and ask the owner.

## Addresses

| What | Where |
|---|---|
| The board | http://localhost:8000 |
| SearXNG | http://localhost:8888 (`/search?q=...&format=json`) |
| Postgres | localhost:5432; as superuser: `docker compose $E exec -T db psql -U collegium` |
| The model | http://localhost:8080/v1 |
| Backups | `~/.collegium/backups` (`COLLEGIUM_BACKUP_DIR`), daily |

## Deploying a change

1. Tests and lint pass: `set -a; . ~/.collegium/.env; set +a; uv run pytest -q` (the tests need `POSTGRES_PASSWORD`), `uv run ruff check . && uv run ruff format .`
2. Commit (when the owner has asked for it).
3. **A migration only with the owner's explicit approval:**
   `docker compose $E run --rm migrate`. Then, if logins changed:
   `docker compose $E run --rm --no-deps logins`.
4. Build: `docker compose $E build worker web scheduler publisher`.
5. Recreate everything except the worker, **always with `--no-deps`**
   (otherwise Compose runs `migrate` as a dependency and applies any
   pending migration unasked):
   `docker compose $E up -d --no-deps web scheduler publisher`
6. Recreate the worker **on its own, in the background**: it finishes its
   current job first (up to `stop_grace_period: 20m`), and Compose waits
   for it, so combining it with the others keeps the board down meanwhile:
   `docker compose $E up -d --no-deps worker` (run in background).
7. SearXNG settings changes: `docker compose $E up -d --no-deps --force-recreate searxng`.

## Looking at the organization

```sh
docker compose $E exec -T web collegium jobs          # recent jobs
docker compose $E exec -T web collegium acquisitions  # external calls
docker compose $E logs --since 10m worker | grep -E "succeeded|failed|deferred"
docker compose $E logs publisher | tail               # what the publisher did
```

Useful queries (as superuser):

```sql
-- what is waiting, and why
SELECT kind, status, count(*), min(run_after), left(last_error, 70)
FROM jobs WHERE status IN ('pending', 'running') GROUP BY 1, 2, 5;

-- paid calls in the last 24 hours, by provider and role
SELECT a.provider, a.capability, ac.name, count(*) FILTER (WHERE a.error IS NULL)
FROM acquisitions a JOIN actors ac ON ac.id = a.requested_by
WHERE a.requested_at > now() - interval '24 hours' GROUP BY 1, 2, 3;
```

Owner commands inside the running system (they act as the owner through
the board login): `docker compose $E exec -T web collegium <command>`, e.g.
`domain sources <slug> searxng hackernews moltbook`.

## Changing operational state by hand

Jobs are not knowledge, but writes still need an actor. As superuser, in
one transaction, declare the system actor first:

```sql
BEGIN;
SELECT set_config('collegium.actor_id', '00000000-0000-0000-0000-000000000001', true);
UPDATE jobs SET run_after = now() WHERE status = 'pending' AND ...;
COMMIT;
```

Never write knowledge (memory tables) by hand; it goes through the roles
or the owner's actions, with provenance.

## Moltbook

- Account `drargus`, the community agent. Its key is
  `COLLEGIUM_MOLTBOOK_API_KEY` in the env file; send it only to
  `https://www.moltbook.com`. Only the `publisher` service gets it.
- Nothing posted there, or on the profile, says that the owner approved it
  (the owner's instruction), although every post is approved.
- Stop switch: `COLLEGIUM_MOLTBOOK_PUBLISHING=off` in the env file, then
  recreate `publisher` and `web`.
- Profile changes by hand: `PATCH /api/v1/agents/me` with the key from the
  env file, only when the owner asks.

## Pushing

See docs/reviews/README.md: nothing is pushed without a PASS security
review. The GitHub repository is https://github.com/ketilaa/collegium
(public).
