# Runbook — running, demoing and debugging

Practical operations guide. For architecture see [architecture.md](architecture.md),
for deployment see [space-cloud-deployment.md](space-cloud-deployment.md).

---

## 0. The one prerequisite that bites

The database lives **inside the Kubernetes cluster**. Nothing on your laptop can
reach it without a port-forward, and that port-forward dies when its terminal
closes, when the pod restarts, or when the laptop sleeps.

**Every failure that looks like "the app is broken" is usually this.**

```bash
kubectl port-forward -n db svc/postgres 55432:5432
```

Leave it running in its own terminal. To check it:

```bash
pgrep -af "port-forward.*postgres"                    # is it running?
PGPASSWORD=mysecretpassword psql -h 127.0.0.1 -p 55432 -U postgres -d newjoiner -c '\dt'
```

To run it detached and survive terminal close:

```bash
nohup kubectl port-forward -n db svc/postgres 55432:5432 > /tmp/pf.log 2>&1 &
```

If the PostgreSQL pod itself is gone:

```bash
kubectl get pods -n db
kubectl -n db logs deploy/postgres --tail=50
```

---

## 1. First-time setup

Already done on this machine — you only need this on a fresh clone.

```bash
cd ~/projects/agentic-access-recommendation-engine

python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

cp .env.example .env      # then set POSTGRES_* (see below)
```

The working `.env` for this environment:

```bash
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=55432          # the port-forward, not 5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=mysecretpassword
POSTGRES_DB=newjoiner
POSTGRES_SSLMODE=disable
DEMO_MODE=true
LLM_PROVIDER=none
MCP_CLIENT_MODE=inmemory
LOG_JSON=false               # human-readable logs; set true for machine parsing
```

Create the schema and load data:

```bash
.venv/bin/alembic upgrade head
.venv/bin/python scripts/seed_database.py
```

---

## 2. Daily start

Two terminals.

**Terminal 1 — the tunnel (leave running):**

```bash
kubectl port-forward -n db svc/postgres 55432:5432
```

**Terminal 2 — the service:**

```bash
cd ~/projects/agentic-access-recommendation-engine
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Confirm it is up:

```bash
curl -s localhost:8000/api/v1/health | python3 -m json.tool
```

`"database": "up"` is the bit that matters. If it says `down`, go back to §0.

| URL | What |
|---|---|
| http://localhost:8000/docs | Swagger UI — clickable, good for demos |
| http://localhost:8000/redoc | ReDoc |
| http://localhost:8000/mcp/ | MCP over Streamable HTTP |

---

## 3. Running a demo

### Option A — the scripted demo (best for showing someone)

Self-contained: runs the whole workflow, prints every governance step, then
demonstrates live MCP tool discovery and invocation. **Does not need the API
running** — only the port-forward.

```bash
.venv/bin/python scripts/run_demo.py --employee EMP1002 --quiet
```

`--quiet` suppresses structured logs so the output is clean for an audience.
Drop it when you want to see the machinery.

```bash
.venv/bin/python scripts/run_demo.py                      # EMP1001, clean path
.venv/bin/python scripts/run_demo.py --employee EMP1003   # multiple SoD conflicts
.venv/bin/python scripts/run_demo.py --all                # all 7 joiners
.venv/bin/python scripts/run_demo.py --skip-mcp-demo      # workflow only
.venv/bin/python scripts/run_demo.py --mcp-mode stdio     # workflow over subprocess MCP
```

### Which joiner tells which story

| Joiner | Shows | Talking point |
|---|---|---|
| `EMP1001` Jane Smith | 8 exact peers, all clean | The happy path; affinity 100 / 87.5 / 75 / 25% |
| `EMP1002` Marcus Chen | **SoD CRITICAL + policy block** | Peers hold a toxic pair; the engine refuses to copy it |
| `EMP1003` Priya Nair | Multiple SoD conflicts, critical-risk item | A manager joiner needs review, not automation |
| `EMP1004` Diego Alvarez | Contractor `DENY` → `REJECTED` | Contract type gates sensitive data; no approval path |
| `EMP1005` Aisha Khan | Location policy block | Data residency: Bangalore is outside approved locations |
| `EMP1006` Tom Becker | **Fallback peer matching** | No one shares his role → relaxes to department+level, confidence drops 0.95 → 0.525 |
| `EMP1007` Lena Rossi | Small clean peer group | Contrast case |

**Strongest 3-minute demo:** `EMP1001` (it works) → `EMP1002` (it catches the
toxic pair) → `EMP1004` (it enforces contractor policy). That arc shows
recommendation, prevention and enforcement.

### Option B — live over the API

```bash
curl -s -X POST localhost:8000/api/v1/joiners/EMP1002/analyze \
  -H 'Content-Type: application/json' -d '{}' | python3 -m json.tool | less
```

Readable summary of just the decisions:

```bash
curl -s -X POST localhost:8000/api/v1/joiners/EMP1002/analyze \
  -H 'Content-Type: application/json' -d '{}' \
| python3 -c "
import json,sys
d=json.load(sys.stdin)
print('analysis:', d['analysis_id'], d['status'], d['summary'])
for r in d['recommendations']:
    print(f\"{r['entitlement_id']:26s} {r['affinity_score']:6.1f}%  risk {r['risk_score']:3d}/{r['risk_level']:8s}  {r['recommendation_status']}\")
"
```

Then show the persisted audit trail and the dashboard:

```bash
curl -s localhost:8000/api/v1/analyses/<analysis_id> | python3 -m json.tool | less
curl -s localhost:8000/api/v1/dashboard | python3 -m json.tool
```

### Option C — through Swagger UI

http://localhost:8000/docs → `POST /api/v1/joiners/{employee_id}/analyze` →
*Try it out* → `EMP1002` → Execute. Good when the audience wants to click.

### Option D — the MCP surface

Prove the tools are real, not decorative:

```bash
.venv/bin/python - <<'EOF'
import asyncio
from fastmcp import Client

async def main():
    async with Client("http://localhost:8000/mcp/") as c:
        tools = await c.list_tools()
        print(f"{len(tools)} tools discovered")
        for t in tools:
            print("  -", t.name)
        r = await c.call_tool("check_sod_conflicts", {
            "employee_id": "EMP1002",
            "entitlement_ids": ["SAP_AP_CREATE_VENDOR", "SAP_AP_APPROVE_PAYMENT"],
        })
        d = r.structured_content
        print("\nSoD:", d["status"], d["severity"], d["conflicts"][0]["sod_id"])

asyncio.run(main())
EOF
```

---

## 4. Debugging

### Watch the workflow think

Run with logs on — every step, every MCP call, every governance evaluation is
logged with the analysis correlation id:

```bash
LOG_JSON=false LOG_LEVEL=INFO .venv/bin/python -c "
from app.agents.graph import run_analysis
r = run_analysis('EMP1002')
print(r.analysis_id, r.status.value)
"
```

Follow one analysis end to end through the API logs:

```bash
# JSON logs, filtered to one correlation id
LOG_JSON=true .venv/bin/uvicorn app.main:app --port 8000 2>&1 \
  | grep '"correlation_id": "<id>"'
```

The correlation id comes back in the `X-Correlation-Id` response header, and you
can set it yourself:

```bash
curl -s -D- -X POST localhost:8000/api/v1/joiners/EMP1001/analyze \
  -H 'X-Correlation-Id: demo-run-1' -H 'Content-Type: application/json' -d '{}' \
  -o /dev/null | grep -i correlation
```

Useful log events to grep for:

| Event | Tells you |
|---|---|
| `workflow.step.start` / `.complete` / `.failed` | Which node, and whether it survived |
| `mcp.call` / `mcp.tool.invoked` / `mcp.tool.completed` | Every MCP round trip |
| `peer_analysis.matched` / `.strategy.empty` | Which strategy won, and which were tried |
| `affinity.calculated` | Candidate count and how many cleared threshold |
| `policy.validated` / `policy.skipped_disabled` | Policy outcome; which policies were disabled |
| `sod.evaluated` | Conflict count and max severity |
| `decision.completed` | Final tally per status |
| `sailpoint.payload_generated` | Included vs excluded counts |

### Interrogate a decision in isolation

Every engine is a plain object — no HTTP, no workflow:

```bash
.venv/bin/python - <<'EOF'
from app.db.session import read_session
from app.services import PeerAnalysisService, AffinityService, RiskService, PolicyService, SodService

with read_session() as s:
    peers = PeerAnalysisService(s).find_peers("EMP1002")
    print("strategy:", peers.matching_strategy.value, "| peers:", peers.peer_ids)

    aff = AffinityService(s).calculate("EMP1002", peers)
    for c in aff.candidates:
        print(f"  {c.entitlement_id:26s} {c.affinity_score:6.2f}%  ({c.peer_count}/{c.total_peers})  threshold={c.meets_threshold}")

    ids = [c.entitlement_id for c in aff.above_threshold()]
    print("policy:", PolicyService(s).validate("EMP1002", ids).status.value)
    print("sod   :", SodService(s).check("EMP1002", ids).status.value)
EOF
```

Ask "why was this blocked?" directly:

```bash
.venv/bin/python - <<'EOF'
from app.db.session import read_session
from app.services import SodService, PolicyService

with read_session() as s:
    sod = SodService(s).check("EMP1002", ["SAP_AP_CREATE_VENDOR", "SAP_AP_APPROVE_PAYMENT"])
    for c in sod.conflicts:
        print(f"{c.sod_id} [{c.severity.value}] {c.entitlement_1} + {c.entitlement_2}")
        print("   ", c.reason)

    pol = PolicyService(s).validate("EMP1002", ["SAP_AP_CREATE_VENDOR"])
    for r in pol.results:
        for p in r.failed_policies:
            print(f"{p.policy_id} [{p.status.value}] {p.policy_name}")
            print("   ", p.reason)
EOF
```

### Query the audit trail

```bash
PGPASSWORD=mysecretpassword psql -h 127.0.0.1 -p 55432 -U postgres -d newjoiner
```

```sql
-- recent analyses
SELECT analysis_id, employee_id, status, matching_strategy, peer_count, candidate_count, started_at
FROM joiner_analyses ORDER BY started_at DESC LIMIT 10;

-- every decision for one joiner, with the reason
SELECT r.entitlement_id, r.affinity_score, r.risk_score, r.risk_level,
       r.policy_status, r.sod_status, r.recommendation_status, r.approval_tier
FROM recommendations r
JOIN joiner_analyses a ON a.analysis_id = r.analysis_id
WHERE a.employee_id = 'EMP1002'
ORDER BY r.affinity_score DESC;

-- why was something blocked?
SELECT r.entitlement_id, s.sod_id, s.severity, s.conflicting_entitlement_id, s.reason
FROM sod_results s JOIN recommendations r ON r.recommendation_id = s.recommendation_id
WHERE r.recommendation_status = 'BLOCKED';

-- the step-by-step decision trace
SELECT entitlement_id, jsonb_pretty(decision_trace)
FROM recommendations WHERE recommendation_status = 'BLOCKED' LIMIT 1;

-- generated SailPoint payloads
SELECT request_id, employee_id, status, entitlement_count, created_at
FROM sailpoint_requests ORDER BY created_at DESC LIMIT 5;

-- the peer evidence behind one recommendation
SELECT e.peer_employee_id, e.evidence_value
FROM recommendation_evidence e
JOIN recommendations r ON r.recommendation_id = e.recommendation_id
WHERE r.entitlement_id = 'SAP_FIN_DISPLAY' LIMIT 10;
```

### Inspect the MCP layer

```bash
# tools + schemas, over a subprocess server (no API needed)
.venv/bin/python - <<'EOF'
from app.agents.mcp_bridge import McpToolInvoker
with McpToolInvoker(mode="stdio") as inv:
    print(inv.list_tools())
    print(inv.call("find_peer_employees", {"employee_id": "EMP1001"})["peer_count"])
EOF
```

Force the workflow through a different transport to isolate an MCP problem:

```bash
MCP_CLIENT_MODE=direct .venv/bin/python scripts/run_demo.py --skip-mcp-demo --quiet
```

If `direct` works and `inmemory`/`stdio` do not, the fault is in the MCP layer,
not the governance logic.

### Tests as a diagnostic

```bash
.venv/bin/python -m pytest                      # all 162
.venv/bin/python -m pytest tests/unit -q        # pure logic, no database
.venv/bin/python -m pytest tests/test_mcp.py -v # MCP protocol
.venv/bin/python -m pytest -m "not slow"        # skip the subprocess test
.venv/bin/python -m pytest -k sod -v            # anything SoD-related
```

`tests/unit` needs no database — if those pass but integration fails, the
problem is connectivity or data, not logic.

---

## 5. Changing behaviour for a demo

All governance thresholds are configuration, so you can show the same joiner
producing different outcomes:

```bash
# Loosen the recommendation threshold: more entitlements get recommended
AFFINITY_THRESHOLD=50 .venv/bin/python scripts/run_demo.py --employee EMP1001 --quiet

# Tighten risk banding: things that were MEDIUM become HIGH and need a manager
RISK_MEDIUM_MAX=40 .venv/bin/python scripts/run_demo.py --employee EMP1003 --quiet
```

### Toggling a control off — proving the block is real

Disabling a control and re-running proves the decision came from a governance
rule rather than a hardcoded special case.

**Clean single-control example — the location policy on `EMP1005`:**

```sql
UPDATE policies SET enabled = false WHERE policy_id = 'POL-005';
```

Re-run `EMP1005`: `WORKDAY_COMP_VIEW` moves from **BLOCKED / HUMAN_REVIEW** to
**MANAGER_APPROVAL** — it is still risk 74 (HIGH), so the risk tier takes over
once the policy stops blocking. Restore with `enabled = true`.

**Layered-controls example — `EMP1002`'s toxic pair.** Note that disabling
`SOD-001` *alone* changes nothing, because `POL-001` blocks the same pair
independently. That is defence in depth working, and it is worth saying out
loud in a demo. To see the fall-through you must disable both:

```sql
UPDATE sod_rules SET enabled = false WHERE sod_id  = 'SOD-001';
UPDATE policies  SET enabled = false WHERE policy_id = 'POL-001';
```

Re-run `EMP1002`: both SAP AP entitlements move from **BLOCKED / HUMAN_REVIEW**
to **MANAGER_APPROVAL** (risk 72 and 88 are HIGH). Restore both afterwards:

```sql
UPDATE sod_rules SET enabled = true WHERE sod_id  = 'SOD-001';
UPDATE policies  SET enabled = true WHERE policy_id = 'POL-001';
```

Both sequences are verified. Always restore the flags — the seed script's
`--purge-all` will also reset them.

---

## 6. Resetting

```bash
.venv/bin/python scripts/seed_database.py --reset       # clear analyses, keep reference data
.venv/bin/python scripts/seed_database.py --purge-all   # clear everything, reseed

# nuclear: drop and rebuild the schema
.venv/bin/alembic downgrade base && .venv/bin/alembic upgrade head
.venv/bin/python scripts/seed_database.py
```

Run `--reset` before a demo so the dashboard numbers start clean.

---

## 7. Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `connection refused` on 55432 | Port-forward died | Restart it (§0) |
| `/health` → `"database": "down"` | Same | Same |
| `psql: database "newjoiner" does not exist` | Wrong database or not created | `CREATE DATABASE newjoiner;` then `alembic upgrade head` |
| `relation "employees" does not exist` | Migrations not applied | `alembic upgrade head` |
| Analysis returns 0 recommendations | Not seeded | `python scripts/seed_database.py` |
| `404 employee_not_found` | Wrong id | `curl localhost:8000/api/v1/joiners` for valid ids |
| `502` / `mcp_tool_error` | MCP transport problem | Try `MCP_CLIENT_MODE=direct` to confirm |
| Integration tests skip | Test DB unreachable | Check port-forward; `newjoiner_test` must exist |
| Uvicorn `exit code 3` | Port already in use | `pkill -f "uvicorn app.main"` or use another port |
| Logs interleave with demo output | Logging enabled | Add `--quiet` |

Health check tells you most of it in one line:

```bash
curl -s localhost:8000/api/v1/health | python3 -m json.tool
```

---

## 8. Quick reference

```bash
# tunnel (always first)
kubectl port-forward -n db svc/postgres 55432:5432

# API + MCP
.venv/bin/uvicorn app.main:app --reload --port 8000

# standalone MCP server
.venv/bin/python -m app.mcp.server --transport stdio
.venv/bin/python -m app.mcp.server --transport http --port 8081

# demo
.venv/bin/python scripts/run_demo.py --employee EMP1002 --quiet

# schema + data
.venv/bin/alembic upgrade head
.venv/bin/python scripts/seed_database.py --reset

# tests
.venv/bin/python -m pytest

# database
PGPASSWORD=mysecretpassword psql -h 127.0.0.1 -p 55432 -U postgres -d newjoiner
```
