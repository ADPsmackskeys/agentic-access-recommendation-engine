#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Container entrypoint.
#
#   api        run migrations (opt-in), then serve FastAPI          [default]
#   mcp        run the MCP server (transport from MCP_TRANSPORT)
#   migrate    run `alembic upgrade head` and exit
#   seed       load the seed corpus and exit
#   demo       run the end-to-end demonstration and exit
#   <other>    executed verbatim
#
# Migrations are opt-in via RUN_MIGRATIONS=true. Schema changes should be a
# deliberate step in a deployment, not a side effect of a pod restart - with
# several replicas, every one of them would race to migrate on boot.
# ---------------------------------------------------------------------------
set -euo pipefail

RUN_MIGRATIONS="${RUN_MIGRATIONS:-false}"
RUN_SEED="${RUN_SEED:-false}"
APP_PORT="${PORT:-8000}"
APP_HOST="${HOST:-0.0.0.0}"
WEB_CONCURRENCY="${WEB_CONCURRENCY:-2}"
DB_WAIT_SECONDS="${DB_WAIT_SECONDS:-60}"

log() { echo "[entrypoint] $*" >&2; }

wait_for_database() {
  log "waiting up to ${DB_WAIT_SECONDS}s for PostgreSQL..."
  local deadline=$((SECONDS + DB_WAIT_SECONDS))
  until python -c "
import sys
from sqlalchemy import create_engine, text
from app.config import get_settings
try:
    engine = create_engine(get_settings().sqlalchemy_url, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))
except Exception as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; do
    if (( SECONDS >= deadline )); then
      log "PostgreSQL was not reachable within ${DB_WAIT_SECONDS}s"
      return 1
    fi
    sleep 2
  done
  log "PostgreSQL is reachable"
}

run_migrations() {
  log "running alembic upgrade head"
  alembic upgrade head
}

run_seed() {
  log "seeding reference data"
  python scripts/seed_database.py
}

case "${1:-api}" in
  api)
    wait_for_database || log "starting anyway; /health will report the database as down"
    [[ "${RUN_MIGRATIONS}" == "true" ]] && run_migrations
    [[ "${RUN_SEED}" == "true" ]] && run_seed
    log "starting FastAPI on ${APP_HOST}:${APP_PORT}"
    exec uvicorn app.main:app \
      --host "${APP_HOST}" \
      --port "${APP_PORT}" \
      --workers "${WEB_CONCURRENCY}" \
      --no-access-log
    ;;
  mcp)
    wait_for_database || true
    log "starting MCP server (transport=${MCP_TRANSPORT:-stdio})"
    exec python -m app.mcp.server
    ;;
  migrate)
    wait_for_database
    run_migrations
    ;;
  seed)
    wait_for_database
    run_seed
    ;;
  demo)
    wait_for_database
    exec python scripts/run_demo.py "${@:2}"
    ;;
  *)
    exec "$@"
    ;;
esac
