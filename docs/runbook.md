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
.venv/bin/python scripts/run_demo.py --employee NJ1007 --quiet
```

`--quiet` suppresses structured logs so the output is clean for an audience.
Drop it when you want to see the machinery.

```bash
.venv/bin/python scripts/run_demo.py                      # NJ1001, clean path
.venv/bin/python scripts/run_demo.py --employee NJ1008    # no peers: recommends nothing
.venv/bin/python scripts/run_demo.py --all                # all 10 joiners
.venv/bin/python scripts/run_demo.py --skip-mcp-demo      # workflow only
.venv/bin/python scripts/run_demo.py --mcp-mode stdio     # workflow over subprocess MCP
```

### Which joiner tells which story

| Joiner | Shows | Talking point |
|---|---|---|
| `NJ1001` Rahul Sharma | 5 exact peers, clean | **The client's own worked example**, reproduced exactly: 100 / 100 / 80 / 20% |
| `NJ1007` Suresh Iyer | **Two human-review holds** | `AUDIT_TOOL` (75) trips their POL005; `SHAREPOINT_AUDIT` is unscored and fails *closed* at 100 |
| `NJ1006` Neha Singh | Thin peer group + boundary | One peer → confidence 0.6175, flagged insufficient; `RSA_GRC` scores exactly 70 and trips POL005 |
| `NJ1010` Arjun Patel | **Fallback peer matching** | Senior Financial Analyst — nobody shares the role *or* the level, so it relaxes all the way to department; confidence 0.8075 → 0.4675 |
| `NJ1008` Deepa Joseph | **No peer group at all** | No HR identities exist, so it recommends *nothing* rather than inventing something |
| `NJ1004` Anjali Rao | Small clean group | `CONFLUENCE_USER` at 66.67% falls below the 70% threshold — the cutoff doing real work |

**Strongest 3-minute demo:** `NJ1001` (it reproduces the client's own numbers) →
`NJ1007` (it fails closed on an entitlement nobody scored) → `NJ1008` (it
declines to guess). That arc shows recommendation, caution and honesty.

> **Know before you demo:** nothing in the client's extract can reach `BLOCKED`
> or `MANAGER_APPROVAL`. Their only policies are two risk thresholds that both
> map to human review, and no identity holds either side of an SoD pair — so
> those controls are correct but inert against this data. If someone asks to see
> an SoD block, use the engine directly (§6) rather than a joiner analysis.

### Option B — live over the API

```bash
curl -s -X POST localhost:8000/api/v1/joiners/NJ1007/analyze \
  -H 'Content-Type: application/json' -d '{}' | python3 -m json.tool | less
```

Readable summary of just the decisions:

```bash
curl -s -X POST localhost:8000/api/v1/joiners/NJ1007/analyze \
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
*Try it out* → `NJ1007` → Execute. Good when the audience wants to click.

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
            "employee_id": "NJ1001",
            "entitlement_ids": ["SAP_VENDOR_CREATE", "SAP_PAYMENT_APPROVER"],
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
r = run_analysis('NJ1007')
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
curl -s -D- -X POST localhost:8000/api/v1/joiners/NJ1001/analyze \
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
    peers = PeerAnalysisService(s).find_peers("NJ1007")
    print("strategy:", peers.matching_strategy.value, "| peers:", peers.peer_ids)

    aff = AffinityService(s).calculate("NJ1007", peers)
    for c in aff.candidates:
        print(f"  {c.entitlement_id:26s} {c.affinity_score:6.2f}%  ({c.peer_count}/{c.total_peers})  threshold={c.meets_threshold}")

    ids = [c.entitlement_id for c in aff.above_threshold()]
    print("policy:", PolicyService(s).validate("NJ1007", ids).status.value)
    print("sod   :", SodService(s).check("NJ1007", ids).status.value)
EOF
```

Ask "why was this blocked?" directly:

```bash
.venv/bin/python - <<'EOF'
from app.db.session import read_session
from app.services import SodService, PolicyService

with read_session() as s:
    sod = SodService(s).check("NJ1001", ["SAP_VENDOR_CREATE", "SAP_PAYMENT_APPROVER"])
    for c in sod.conflicts:
        print(f"{c.sod_id} [{c.severity.value}] {c.entitlement_1} + {c.entitlement_2}")
        print("   ", c.reason)

    pol = PolicyService(s).validate("NJ1001", ["SAP_PAYMENT_APPROVER"])
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
WHERE a.employee_id = 'NJ1007'
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
    print(inv.call("find_peer_employees", {"employee_id": "NJ1001"})["peer_count"])
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
AFFINITY_THRESHOLD=50 .venv/bin/python scripts/run_demo.py --employee NJ1001 --quiet

# Tighten risk banding: things that were MEDIUM become HIGH and need a manager
RISK_MEDIUM_MAX=40 .venv/bin/python scripts/run_demo.py --employee NJ1006 --quiet
```

### Toggling a control off — proving the block is real

Disabling a control and re-running proves the decision came from a governance
rule rather than a hardcoded special case.

Use `NJ1007`, whose three entitlements sit in three different bands. Baseline:

```
SHAREPOINT_AUDIT   risk=100   HUMAN_REVIEW
AUDIT_TOOL         risk= 75   HUMAN_REVIEW
POWERBI_AUDIT      risk= 10   AUTO_APPROVED
```

**Disable the client's risk-review policy:**

```sql
UPDATE policies SET enabled = false WHERE policy_id = 'POL005';
```

Re-run `NJ1007`: `AUDIT_TOOL` moves from **HUMAN_REVIEW** to
**MANAGER_APPROVAL**. It is still risk 75 (HIGH), so the risk band takes over
once the policy stops demanding review — the decision fell through to a weaker
control rather than disappearing. *(This is also the only way to see
`MANAGER_APPROVAL` on this dataset at all.)*

**Now disable the critical-access policy too:**

```sql
UPDATE policies SET enabled = false WHERE policy_id = 'POL006';
```

Re-run `NJ1007`: **`SHAREPOINT_AUDIT` does not move.** It is still held at
`HUMAN_REVIEW` with both policies off, because its risk of 100 puts it in the
CRITICAL band, and the risk tier is an independent control. That is defence in
depth working, and it is worth saying out loud in a demo — especially since
`SHAREPOINT_AUDIT` has no risk score in the client's extract at all and is
scored 100 precisely so it fails closed.

Restore afterwards:

```sql
UPDATE policies SET enabled = true;
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
| `NJ1008` returns nothing and status `FAILED` | Correct behaviour — no HR identities exist to learn from | Nothing to fix; this is the honest case |
| Demo hangs for minutes, `503 UNAVAILABLE` / `504 DEADLINE_EXCEEDED` | `DEMO_MODE=false` and the LLM provider is throttling | Set `DEMO_MODE=true`. The decisions are identical either way — only the prose changes |
| `API_KEY_INVALID` **after** you fixed the key in `.env` | The running process still holds the old one — see below | Restart the server |
| Seed counts look short (e.g. 10 risk scores, not 15) | `seed/*.json` is stale or was partially written | `python scripts/convert_client_csv.py --check`, then re-run it without `--check` |
| `404 employee_not_found` | Wrong id | `curl localhost:8000/api/v1/joiners` for valid ids |
| `502` / `mcp_tool_error` | MCP transport problem | Try `MCP_CLIENT_MODE=direct` to confirm |
| Integration tests skip | Test DB unreachable | Check port-forward; `newjoiner_test` must exist |
| Uvicorn `exit code 3` | Port already in use | `pkill -f "uvicorn app.main"` or use another port |
| Logs interleave with demo output | Logging enabled | Add `--quiet` |
| `/mcp` in a browser → `-32600 Not Acceptable: Client must accept text/event-stream` | **Not a fault.** See below | Use an MCP client, not a browser |

### The LLM is slow or times out

An analysis makes roughly five model calls (one summary plus one per candidate
entitlement), so per-call latency multiplies. Measured on a trivial prompt:

| Model | Round trip |
|---|---|
| `gemma-4-31b-it` | ~31s, with frequent `504 DEADLINE_EXCEEDED` / `503 UNAVAILABLE` |
| `gemini-2.5-flash` | ~6s |
| `gemini-3.5-flash` | ~2s |

At 30s a call, an analysis exceeds `MCP_CLIENT_TIMEOUT_SECONDS` (120) and the
explanation node fails — the decisions survive, but the prose falls back. The
Gemma models on the Gemini API are large and heavily contended; they are a poor
fit for a per-entitlement call pattern.

**Fix:** use a flash model.

```bash
LLM_MODEL=gemini-3.5-flash
```

If you must keep Gemma, raise `MCP_CLIENT_TIMEOUT_SECONDS` to ~600 and expect a
multi-minute demo.

`LLM_MAX_RETRIES` (default 2) bounds the provider SDK's internal retry loop.
`LLM_TIMEOUT_SECONDS` bounds a *single attempt*, not the loop — the SDK default
of six retries is why an overloaded model could previously hold the workflow
open for minutes.

### Editing `.env` does not affect a running server

`get_settings()` is `@lru_cache(maxsize=1)`, so `.env` is read **once** when the
process starts and the values are pinned for its lifetime. `uvicorn --reload`
watches `.py` files, so editing `.env` does not trigger a restart either — the
server keeps serving with the old configuration indefinitely.

The symptom that gives it away: a fresh process works and the long-running one
does not. To confirm which you are looking at:

```bash
ps -eo pid,lstart,cmd | grep "[u]vicorn app.main"     # when did it start?
```

If it started before you edited `.env`, that is the whole problem. Restart it.

To make `.env` edits reload automatically:

```bash
.venv/bin/uvicorn app.main:app --reload --reload-include '.env' --port 8000
```

Verified: touching `.env` then logs `WatchFiles detected changes in '.env'.
Reloading...` and the new process picks up the new values.

### `/mcp` is not browsable

Opening `http://localhost:8000/mcp` in a browser returns HTTP 406 and:

```json
{"jsonrpc":"2.0","id":"server-error",
 "error":{"code":-32600,"message":"Not Acceptable: Client must accept text/event-stream"}}
```

This is the MCP Streamable HTTP transport behaving correctly. The endpoint
speaks JSON-RPC over `POST` and replies as a Server-Sent Events stream, so the
spec requires the client to send:

```
Accept: application/json, text/event-stream
```

A browser sends `Accept: text/html,...`, so the server correctly refuses. There
is nothing to fix — the endpoint is for MCP clients, not for reading.

To check it is alive, send a real handshake:

```bash
curl -s -X POST http://localhost:8000/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
       "protocolVersion":"2025-06-18","capabilities":{},
       "clientInfo":{"name":"curl","version":"1"}}}'
```

A healthy server replies `event: message` with its `serverInfo` and
capabilities. Easier still, use the client:

```bash
.venv/bin/python -c "
import asyncio
from fastmcp import Client
async def main():
    async with Client('http://localhost:8000/mcp/') as c:
        print([t.name for t in await c.list_tools()])
asyncio.run(main())"
```

The human-readable surface is `/docs` (Swagger). `/mcp` has no browser UI by
design.

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
.venv/bin/python scripts/run_demo.py --employee NJ1007 --quiet

# schema + data
.venv/bin/alembic upgrade head
.venv/bin/python scripts/convert_client_csv.py --check   # JSON matches the CSVs?
.venv/bin/python scripts/seed_database.py --reset

# tests
.venv/bin/python -m pytest

# database
PGPASSWORD=mysecretpassword psql -h 127.0.0.1 -p 55432 -U postgres -d newjoiner
```
