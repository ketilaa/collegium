#!/usr/bin/env bash
# The last guard before a production deploy: brings up the whole pipeline
# (Postgres, a real small model, stubbed Tavily/Moltbook) in its own
# throwaway Compose project, runs scout -> research -> review -> record
# and a strategize cycle through it for real, and fails if anything
# landed in `jobs.status = 'failed'`. See docs/operations.md, "Local
# testing against a small model".
#
#   scripts/localtest-smoke.sh
#
# Needs .env.localtest (copy .env.localtest.example). First run pulls the
# model from Hugging Face; later runs use the cached copy.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

PROJECT=collegium-localtest
DOMAIN=localtest-smoke
COMPOSE=(docker compose -p "$PROJECT" --env-file .env.localtest --profile localtest)

cleanup() {
    echo "--- tearing down ($PROJECT) ---"
    "${COMPOSE[@]}" down
}
trap cleanup EXIT

echo "--- starting from a clean database (keeping the cached model) ---"
"${COMPOSE[@]}" down >/dev/null 2>&1 || true
docker volume rm -f "${PROJECT}_db-data" >/dev/null 2>&1 || true

echo "--- starting db, llm and the stub server ---"
"${COMPOSE[@]}" up -d --wait db llm stubs

echo "--- migrating and creating logins ---"
"${COMPOSE[@]}" run --rm --no-deps migrate
"${COMPOSE[@]}" run --rm --no-deps logins

cli() { "${COMPOSE[@]}" run --rm --no-deps web collegium "$@"; }

echo "--- setting up the test domain (Tavily only, pointed at the stub) ---"
cli domain add "$DOMAIN" "Local smoke test domain"
cli domain sources "$DOMAIN" tavily
cli scout "$DOMAIN"

drain() {
    for _ in $(seq 1 "$1"); do
        "${COMPOSE[@]}" run --rm --no-deps worker collegium worker --drain
        pending=$(
            "${COMPOSE[@]}" exec -T db psql -U collegium -d collegium -t -A \
                -c "SELECT count(*) FROM jobs WHERE status IN ('pending', 'running')"
        )
        [ "$pending" -eq 0 ] && return 0
    done
}

echo "--- draining the research pipeline ---"
drain 10

echo "--- a strategize cycle (exercises program-duplicate detection too) ---"
cli strategize "$DOMAIN"
drain 5

echo "--- checking for failed jobs ---"
failed=$(
    "${COMPOSE[@]}" exec -T db psql -U collegium -d collegium -t -A \
        -c "SELECT kind || ': ' || coalesce(last_error, '(no error recorded)') FROM jobs WHERE status = 'failed'"
)
if [ -n "$failed" ]; then
    echo "FAILED jobs:"
    echo "$failed"
    exit 1
fi

echo "--- summary ---"
cli jobs
cli hypotheses
echo "PASS: no failed jobs."
