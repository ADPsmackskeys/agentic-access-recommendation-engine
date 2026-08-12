# Space-Cloud deployment

Space Cloud is the target runtime, and PostgreSQL is provided by the
Space-Cloud PostgreSQL add-on. The application assumes the database is
**external and already running** — it never provisions one, and there is no
embedded or fallback database of any kind.

Everything below is environment-variable driven, so the same container image
runs unchanged locally and on Space Cloud.

---

## 1. Prerequisites

```bash
space-cli --version          # tested against 0.21.5
kubectl cluster-info
```

Install the PostgreSQL add-on if it is not already present:

```bash
space-cli add database postgres --name postgres
```

This deploys PostgreSQL into the `db` namespace and exposes it in-cluster as:

```
postgres.db.svc.cluster.local:5432
```

Verify:

```bash
kubectl get pods -n db
kubectl get svc  -n db
```

Create the application database once (the add-on ships only `postgres`):

```bash
kubectl -n db exec deploy/postgres -- \
  psql -U postgres -c "CREATE DATABASE newjoiner;"
```

---

## 2. Required environment variables

### Database — required

| Variable | Example | Notes |
|---|---|---|
| `POSTGRES_HOST` | `postgres.db.svc.cluster.local` | Space-Cloud add-on service name |
| `POSTGRES_PORT` | `5432` | |
| `POSTGRES_USER` | `postgres` | |
| `POSTGRES_PASSWORD` | *(secret)* | Never commit this |
| `POSTGRES_DB` | `newjoiner` | |
| `POSTGRES_SSLMODE` | `disable` | `require` when TLS terminates at the database |

Alternatively supply a single `DATABASE_URL`; it takes precedence and is
normalised onto the `postgresql+psycopg://` driver. A URL pointing at anything
other than PostgreSQL is rejected at start-up rather than silently accepted.

### Application

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `local` | Set to `production` |
| `DEBUG` | `false` | Keep false in production |
| `API_PREFIX` | `/api/v1` | |
| `LOG_LEVEL` | `INFO` | |
| `LOG_JSON` | `true` | Structured logs to **stderr** |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated; empty disables cross-origin |
| `MAX_REQUEST_BYTES` | `1048576` | Request body ceiling |

### Governance thresholds

| Variable | Default |
|---|---|
| `AFFINITY_THRESHOLD` | `70.0` |
| `RISK_LOW_MAX` | `30` |
| `RISK_MEDIUM_MAX` | `69` |
| `RISK_HIGH_MAX` | `89` |
| `PEER_CONFIDENCE_SATURATION` | `8` |
| `MIN_PEER_COUNT` | `2` |
| `SAILPOINT_INCLUDED_STATUSES` | `AUTO_APPROVED,MANAGER_APPROVAL` |

Start-up validates that `RISK_LOW_MAX < RISK_MEDIUM_MAX < RISK_HIGH_MAX`; a
misordered configuration fails fast rather than silently misclassifying risk.

### LLM and demo mode

| Variable | Default | Notes |
|---|---|---|
| `DEMO_MODE` | `true` | `true` ⇒ fully deterministic, no LLM contacted, whatever else is set |
| `LLM_PROVIDER` | `none` | `none` \| `anthropic` \| `openai` |
| `LLM_MODEL` | `claude-sonnet-5` | |
| `LLM_API_KEY` | *(unset)* | Supply via a secret; never logged |

Leaving `DEMO_MODE=true` is a valid production posture for this MVP: the
governance decisions are identical either way, and only the wording of the
explanations changes.

### MCP

| Variable | Default | Notes |
|---|---|---|
| `MCP_CLIENT_MODE` | `inmemory` | How the workflow reaches its tools |
| `MCP_TRANSPORT` | `stdio` | Transport for a standalone `python -m app.mcp.server` |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8081` | Standalone HTTP transport |
| `MCP_SERVER_NAME` | `agentic-access-recommendation-engine` | Advertised server name |

The API already serves MCP over Streamable HTTP at `/mcp`, so a separate MCP
process is optional.

### Container behaviour

| Variable | Default | Notes |
|---|---|---|
| `RUN_MIGRATIONS` | `false` | Keep false; migrate with the Job below |
| `RUN_SEED` | `false` | |
| `PORT` / `HOST` | `8000` / `0.0.0.0` | |
| `WEB_CONCURRENCY` | `2` | Uvicorn workers |
| `DB_WAIT_SECONDS` | `60` | Start-up wait for PostgreSQL |

---

## 3. Build and publish the image

```bash
docker build -t ghcr.io/your-org/agentic-access-recommendation-engine:0.1.0 .
docker push ghcr.io/your-org/agentic-access-recommendation-engine:0.1.0
```

Update the image reference in `deploy/space-cloud-service.yaml` and
`deploy/kubernetes.yaml`.

---

## 4. Deploy

Two supported routes. They deploy the same image with the same configuration.

### Route A — `space-cli` (Space Cloud service)

```bash
space-cli login
space-cli apply -f deploy/space-cloud-service.yaml --project newjoiner
space-cli apply -f deploy/space-cloud-ingress.yaml --project newjoiner
```

`deploy/space-cloud-service.yaml` declares the service, its scaling profile,
its ports and its full environment. Replace `POSTGRES_PASSWORD: CHANGE_ME` with
a Space Cloud secret reference before using it anywhere real.

> The service manifest matches the Space Cloud 0.21 service schema. It has not
> been applied against a logged-in Space Cloud project as part of this build —
> `space-cli` requires an authenticated account — so treat the first
> `space-cli apply` as the verification step. Route B has been validated
> against the cluster with a server-side dry run.

### Route B — plain Kubernetes manifests

Space Cloud runs on Kubernetes, so the manifests can be applied directly. This
route runs migrations as a first-class Job with its own lifecycle:

```bash
kubectl apply -f deploy/kubernetes.yaml

kubectl -n newjoiner wait --for=condition=complete job/newjoiner-migrate --timeout=180s
kubectl -n newjoiner rollout status deploy/newjoiner-api
```

Before applying, set the real password:

```bash
kubectl -n newjoiner create secret generic newjoiner-db \
  --from-literal=POSTGRES_PASSWORD='...' \
  --from-literal=LLM_API_KEY='' \
  --dry-run=client -o yaml | kubectl apply -f -
```

---

## 5. Migration process

```bash
alembic upgrade head
```

In the container this is `entrypoint.sh migrate`, and in Kubernetes it is the
`newjoiner-migrate` Job.

**Migrations do not run on pod start by default.** `RUN_MIGRATIONS` defaults to
`false` because with more than one replica every replica would race to migrate
on boot. Run the Job, wait for it to complete, then roll out the API.

The connection URL always comes from application settings, never from
`alembic.ini`, so `alembic upgrade head` behaves identically locally, in Docker
and on Space Cloud.

---

## 6. Seed process

```bash
python scripts/seed_database.py               # idempotent upsert
python scripts/seed_database.py --reset       # wipe analyses, keep reference data
python scripts/seed_database.py --purge-all   # wipe everything, then reseed
```

In the container: `entrypoint.sh seed`. The seed corpus is deterministic —
44 active employees, 7 new joiners, 2 leavers, 24 entitlements, 8 policies and
8 SoD rules — so the affinity percentages in the README and tests are
reproducible.

Re-running plain `seed` upserts reference data and leaves analysis output
untouched.

---

## 7. Startup commands

| Purpose | Command |
|---|---|
| API (default) | `entrypoint.sh api` → `uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| MCP server | `entrypoint.sh mcp` → `python -m app.mcp.server` |
| Migrate | `entrypoint.sh migrate` |
| Seed | `entrypoint.sh seed` |
| Demo | `entrypoint.sh demo` |

---

## 8. Health check

```
GET /api/v1/health
```

Returns **200** when PostgreSQL is reachable and **503** when it is not, so it
works directly as a readiness probe:

```json
{
  "status": "ok",
  "service": "Agentic Access Recommendation Engine",
  "environment": "production",
  "version": "0.1.0",
  "database": "up",
  "demo_mode": true,
  "llm_enabled": false,
  "mcp_client_mode": "inmemory",
  "timestamp": "2026-08-12T00:00:00Z"
}
```

Readiness uses `/api/v1/health` (database-aware); liveness uses `/` (process
only), so a database blip drains traffic instead of restarting healthy pods.

---

## 9. Verify the deployment

```bash
kubectl -n newjoiner port-forward svc/newjoiner-api 8000:8000

curl -s localhost:8000/api/v1/health
curl -s localhost:8000/api/v1/joiners
curl -s -X POST localhost:8000/api/v1/joiners/NJ1001/analyze -d '{}' \
     -H 'Content-Type: application/json'
curl -s localhost:8000/api/v1/dashboard
```

MCP over the same endpoint:

```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("http://localhost:8000/mcp/") as client:
        print([t.name for t in await client.list_tools()])

asyncio.run(main())
```

---

## 10. Connecting from a workstation

The database is in-cluster only. To run migrations, seeding or the demo from a
laptop, forward the port:

```bash
kubectl port-forward -n db svc/postgres 55432:5432
```

then point the application at it:

```bash
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=55432
POSTGRES_SSLMODE=disable
```

This is exactly how the local `.env` in this repository is configured.

---

## 11. Operational notes

- **Secrets.** Nothing credential-shaped is in source or baked into the image.
  The database URL is redacted before it is logged, and the log pipeline blanks
  any field key-named like a secret.
- **Scaling.** The API is stateless; scale replicas freely. Keep
  `RUN_MIGRATIONS=false` when doing so.
- **Connection pool.** `DB_POOL_SIZE` (default 5) and `DB_MAX_OVERFLOW`
  (default 10) are per replica. Size them against the PostgreSQL add-on's
  `max_connections` before scaling out.
- **Docker Compose is not used in production.** `docker-compose.yml` is for
  local development only; on Space Cloud the database is the add-on.
