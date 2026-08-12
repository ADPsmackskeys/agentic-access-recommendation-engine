# Agentic Access Recommendation Engine

Backend MVP for AI-assisted joiner access provisioning. Given a new employee,
it works out which application entitlements they should receive by analysing
comparable peers, then puts every candidate through the controls a human access
reviewer would apply — risk banding, policy validation and segregation of
duties — before producing an explainable decision and a SailPoint
IdentityIQ-style access request.

**Backend only.** The REST API and the MCP tool surface are the interfaces.

---

## Table of contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Environment variables](#environment-variables)
- [Local development](#local-development)
- [PostgreSQL setup](#postgresql-setup)
- [Database migration](#database-migration)
- [Database seeding](#database-seeding)
- [Running FastAPI](#running-fastapi)
- [Running the MCP server](#running-the-mcp-server)
- [Running the demo](#running-the-demo)
- [API examples](#api-examples)
- [MCP examples](#mcp-examples)
- [LangGraph workflow](#langgraph-workflow)
- [Governance decision logic](#governance-decision-logic)
- [SailPoint simulation](#sailpoint-simulation)
- [Testing](#testing)
- [Docker](#docker)
- [Space-Cloud deployment](#space-cloud-deployment)
- [Project structure](#project-structure)
- [Design notes](#design-notes)
- [Future enhancements](#future-enhancements)

---

## Overview

```
New joiner → identity profiling → peer analysis → entitlement affinity
          → risk evaluation → policy validation → SoD validation
          → explainable decision → SailPoint request payload
```

The governing principle, and the reason to trust the output:

> **The LLM never decides anything.** Peer selection, affinity, risk
> classification, policy evaluation, SoD detection, approval tier and the final
> recommendation status are computed by deterministic rules over database
> state. The model layer turns that structured evidence into readable prose,
> and cannot alter a decision.

Every analysis — the decision, the peer evidence behind it, the policies that
fired, the SoD rules that matched, both halves of the explanation and the
generated request payload — is persisted to PostgreSQL and retrievable months
later.

### Technology

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 · Alembic · psycopg 3 ·
PostgreSQL (on Space-Cloud) · LangGraph · LangChain · MCP Python SDK · FastMCP ·
structlog · pytest

---

## Architecture

```mermaid
flowchart TD
    CLIENT[REST client] --> API[FastAPI]
    AGENT[MCP client] --> MCPS[FastMCP server]
    API -. mounts .-> MCPS
    API --> GRAPH[LangGraph workflow]
    GRAPH -->|MCP protocol| MCPS
    MCPS --> SERVICES[Domain services]
    GRAPH -->|in-process| KERNEL[Decision engine]
    KERNEL --> SERVICES
    SERVICES --> DB[(PostgreSQL · Space-Cloud)]
    SERVICES --> LLM[LLM abstraction]
    SERVICES --> SP[SailPoint payload generator]

    style KERNEL fill:#f9e79f,stroke:#b7950b
    style DB fill:#d4e6f1,stroke:#2874a6
```

REST and MCP are two transports over **one** service layer — no business rule
is written twice. Full detail, including the data model, decision flow and
MCP design tradeoffs: **[docs/architecture.md](docs/architecture.md)**.

---

## Prerequisites

- Python 3.12+
- PostgreSQL 14+ (the Space-Cloud add-on, a container, or any reachable instance)
- Docker, only if you want the containerised stack

No LLM API key is needed. `DEMO_MODE=true` (the default) keeps the whole system
deterministic and runnable with no external AI dependency.

---

## Environment variables

Copy `.env.example` to `.env` and adjust. Every setting is environment-driven;
nothing credential-shaped lives in source.

```bash
cp .env.example .env
```

The essentials:

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_HOST` / `PORT` / `USER` / `PASSWORD` / `DB` | `localhost` / `5432` / `postgres` / — / `newjoiner` | Database connection |
| `DATABASE_URL` | *(unset)* | Full URL; overrides the parts above |
| `AFFINITY_THRESHOLD` | `70.0` | Minimum affinity % to recommend |
| `RISK_LOW_MAX` / `RISK_MEDIUM_MAX` / `RISK_HIGH_MAX` | `30` / `69` / `89` | Risk band bounds |
| `DEMO_MODE` | `true` | `true` ⇒ deterministic, no LLM contacted |
| `LLM_PROVIDER` | `none` | `none` \| `anthropic` \| `openai` \| `google` |
| `LLM_MODEL` | `claude-sonnet-5` | e.g. `gemma-4-31b-it`, `gemini-2.5-pro` |
| `CHAT_LLM_MODEL` | *(unset)* | Model for `/chat`; falls back to `LLM_MODEL` |
| `LLM_API_KEY` | *(unset)* | Only when `DEMO_MODE=false` |
| `MCP_CLIENT_MODE` | `inmemory` | How the workflow reaches its MCP tools |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | Structured logging (to stderr) |

The full list is in [`.env.example`](.env.example) and
[docs/space-cloud-deployment.md](docs/space-cloud-deployment.md#2-required-environment-variables).

---

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env      # then point POSTGRES_* at your database

alembic upgrade head
python scripts/seed_database.py

uvicorn app.main:app --reload
```

---

## PostgreSQL setup

PostgreSQL is the only supported database. There is no SQLite fallback anywhere
in the project, including its test suite.

**Space-Cloud (the production target).** The database is the Space-Cloud
PostgreSQL add-on:

```bash
space-cli add database postgres --name postgres
kubectl -n db exec deploy/postgres -- psql -U postgres -c "CREATE DATABASE newjoiner;"
```

In-cluster it resolves as `postgres.db.svc.cluster.local:5432`. To reach it
from a workstation:

```bash
kubectl port-forward -n db svc/postgres 55432:5432
```

```bash
# .env
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=55432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<add-on password>
POSTGRES_DB=newjoiner
POSTGRES_SSLMODE=disable
```

**Local container** (development only):

```bash
docker compose up -d postgres
```

---

## Database migration

```bash
alembic upgrade head        # apply
alembic downgrade -1        # roll back one
alembic current             # show applied revision
```

The connection URL comes from application settings, not from `alembic.ini`, so
the same command works locally, in Docker and on Space Cloud.

---

## Database seeding

The seed corpus is the **client's own extract**. `seed/client/*.csv` is what they
sent; `seed/*.json` is the ground truth the project loads. Two steps, kept
separate so the transliteration can be re-run and diffed without touching the
mapping:

```bash
python scripts/convert_client_csv.py          # seed/client/*.csv -> seed/*.json
python scripts/convert_client_csv.py --check  # verify the JSON still matches

python scripts/seed_database.py               # idempotent upsert
python scripts/seed_database.py --reset       # wipe analyses, keep reference data
python scripts/seed_database.py --purge-all   # wipe everything, then reseed
```

`seed_database.py` prints the data-quality findings it hits — unscored
entitlements, unresolvable manager ids, policy rules with no implemented
evaluator — rather than swallowing them.

The corpus is deterministic — no randomness — so the affinity percentages are
reproducible:

| Data | Count |
|---|---|
| Active identities | 10 |
| New joiners (`PENDING_START`) | 10 |
| Entitlements across 8 applications | 16 |
| Access grants | 29 |
| Policies loaded (of 7 supplied) | 2 |
| SoD rules | 3 |

The joiners exercise different governance paths:

| Joiner | Scenario | Outcome |
|---|---|---|
| `NJ1001` Rahul Sharma | Financial Analyst, 5 exact peers | Clean auto-approval — the client's own worked example |
| `NJ1004` Anjali Rao | Software Engineer, 3 exact peers | Auto-approval; `CONFLUENCE_USER` excluded at 66.67% |
| `NJ1006` Neha Singh | Risk Analyst, a single peer | Thin-cohort warning; `RSA_GRC` (70) → `HUMAN_REVIEW` |
| `NJ1007` Suresh Iyer | Internal Auditor | `AUDIT_TOOL` (75) and unscored `SHAREPOINT_AUDIT` → `HUMAN_REVIEW` |
| `NJ1008` Deepa Joseph | HR Specialist, no HR identities exist | No peer group; recommends nothing |
| `NJ1009` Vivek Kumar | Cloud Engineer | Fallback to department matching |
| `NJ1010` Arjun Patel | Senior Financial Analyst | Fallback to department matching |

The joiner routing rule, as it runs today:

| Risk band | Outcome | On the ticket? |
|---|---|---|
| LOW (0–30) / MEDIUM (31–69) | `AUTO_APPROVED` | Yes — provisioned without a human |
| HIGH (70–89) | `MANAGER_APPROVAL` | Yes — flagged for the line manager |
| CRITICAL (90–100) | `HUMAN_REVIEW` | No — withheld for governance |
| Affinity below 70% | `NOT_RECOMMENDED` | No |

The tier for a risk-threshold policy is derived from where its threshold sits in
the configured bands, because the client's `policy_rules.csv` types both `POL005`
(≥70) and `POL006` (≥90) as the same undifferentiated `HUMAN_APPROVAL`. That maps
`POL005` to manager approval and `POL006` to human review.

One property of this corpus is worth knowing before demoing it:

- **No outcome reaches `BLOCKED`.** No identity holds either side of an SoD
  pair, and the client supplied no blocking policies — so that control is
  correct but inert against this data. See
  [docs/client-data-assessment.md](docs/client-data-assessment.md) §3.1.
- **Four of their seven policy rules are birthright grants** and are reported,
  not loaded: every implemented evaluator is a restriction, and loading a grant
  as one would invert its meaning.

---

## Running FastAPI

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

| Endpoint | Purpose |
|---|---|
| `/docs` | Swagger UI |
| `/redoc` | ReDoc |
| `/openapi.json` | OpenAPI schema |
| `/mcp/` | MCP over Streamable HTTP |

---

## Running the MCP server

```bash
python -m app.mcp.server --transport stdio
python -m app.mcp.server --transport http --host 0.0.0.0 --port 8081
```

The API already serves MCP at `/mcp`, so a separate process is optional. See
**[docs/mcp.md](docs/mcp.md)** for client configuration, discovery and
invocation.

---

## Running the demo

```bash
python scripts/run_demo.py                      # NJ1001, the clean path
python scripts/run_demo.py --employee NJ1007    # human-review path
python scripts/run_demo.py --employee NJ1008    # no peers: recommends nothing
python scripts/run_demo.py --all                # every seeded joiner
python scripts/run_demo.py --mcp-mode stdio     # workflow over subprocess MCP
```

It runs the complete workflow, prints each governance step, then demonstrates
live MCP tool discovery and invocation against a subprocess server.

```
==============================================================================
                     AGENTIC ACCESS RECOMMENDATION ENGINE
==============================================================================
Environment       : local
Database          : postgresql+psycopg://postgres:***@127.0.0.1:55432/newjoiner
Demo mode         : True
LLM               : deterministic (no LLM)
MCP client mode   : inmemory
Affinity threshold: 70.0%

==============================================================================
NEW JOINER
==============================================================================
Employee   : NJ1007 - Suresh Iyer
Role       : Internal Auditor
Department : Audit
Level      : L3
Location   : Mumbai

==============================================================================
PEER ANALYSIS
==============================================================================
Matching strategy : job_role_department_job_level
Strategies tried  : job_role_department_job_level
Peers matched     : 1
Confidence        : 0.6175

Peer group:
  EMP010   John                     Internal Auditor              3 entitlements

==============================================================================
RECOMMENDATIONS
==============================================================================
ENTITLEMENT                   AFFINITY       RISK POLICY         SOD        DECISION
------------------------------------------------------------------------------
AUDIT_TOOL                      100.0%    75/HIGH REQUIRES_APPROVAL PASS    HUMAN_REVIEW
SHAREPOINT_AUDIT                100.0% 100/CRITICAL REQUIRES_APPROVAL PASS  HUMAN_REVIEW
POWERBI_AUDIT                   100.0%     10/LOW PASS           PASS       AUTO_APPROVED
------------------------------------------------------------------------------
Summary: 1 AUTO_APPROVED, 2 HUMAN_REVIEW

==============================================================================
CONTROLS THAT FIRED
==============================================================================

AUDIT_TOOL -> HUMAN_REVIEW
  Human review required by policy: Risk Review: entitlements scoring 70 or
  above require human review. Risk score 75 meets the policy threshold of 70.
  Pol  POL005 [REQUIRES_APPROVAL] Risk Review

SHAREPOINT_AUDIT -> HUMAN_REVIEW
  Human review required: risk score 100 is classified as CRITICAL (90-100).
  Pol  POL005 [REQUIRES_APPROVAL] Risk Review
  Pol  POL006 [REQUIRES_APPROVAL] Critical Access
```

`SHAREPOINT_AUDIT` is the interesting one: it appears in the client's affinity
table and is held by an existing auditor, but it has **no risk score anywhere in
their extract**. It is loaded as 100/CRITICAL so it fails closed — an
entitlement nobody has scored is not automatically a safe one.

---

## API examples

### Run an analysis

```bash
curl -X POST http://localhost:8000/api/v1/joiners/EMP1001/analyze \
  -H 'Content-Type: application/json' -d '{}'
```

```json
{
  "analysis_id": "abf79a9a-f255-4b02-a300-9617815b842f",
  "status": "COMPLETED",
  "employee": { "employee_id": "EMP1001", "name": "Jane Smith", "job_role": "Financial Analyst" },
  "peer_analysis": {
    "matching_strategy": "job_role_department_job_level",
    "peer_count": 8,
    "confidence": 0.95
  },
  "recommendations": [
    {
      "entitlement_id": "SAP_FIN_DISPLAY",
      "affinity_score": 100.0,
      "peer_count": 8,
      "total_peers": 8,
      "risk_score": 15,
      "risk_level": "LOW",
      "policy_status": "PASS",
      "sod_status": "PASS",
      "recommendation_status": "AUTO_APPROVED",
      "approval_tier": "AUTO",
      "reason": "Auto-approved: 8 of 8 matched peers hold this entitlement (100.0% affinity), risk score 15 is LOW, all policies passed and no SoD conflicts were detected."
    }
  ],
  "sailpoint_payload": { "status": "SIMULATED", "identity": "EMP1001" },
  "summary": { "AUTO_APPROVED": 6, "NOT_RECOMMENDED": 3 }
}
```

### Everything else

```bash
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/joiners
curl http://localhost:8000/api/v1/joiners/EMP1001
curl http://localhost:8000/api/v1/analyses/<analysis_id>
curl http://localhost:8000/api/v1/dashboard

curl -X POST http://localhost:8000/api/v1/access-requests \
  -H 'Content-Type: application/json' \
  -d '{"analysis_id": "<analysis_id>"}'
```

```json
{
  "total_joiners": 7,
  "total_analyses": 2,
  "total_recommendations": 17,
  "auto_approved": 10,
  "manager_approval": 0,
  "human_review": 0,
  "blocked": 2,
  "not_recommended": 5,
  "high_risk": 2,
  "critical_risk": 0
}
```

Errors are structured, with the domain code preserved across the MCP boundary:

```json
{
  "error": "employee_not_found",
  "message": "Employee 'NOPE' does not exist.",
  "details": { "employee_id": "NOPE" }
}
```

### Ask a question

```bash
curl -s -X POST localhost:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"question": "Which entitlements have a risk score of 70 or more?"}'
```

```json
{
  "answer": "AD_DOMAIN_ADMIN in Active Directory has a risk score of 100. AUDIT_TOOL in Audit Platform has 75, RSA_GRC in RSA Archer has 70, SAP_PAYMENT_APPROVER has 95, SAP_VENDOR_CREATE has 90, and SHAREPOINT_AUDIT has 100.",
  "sql": "SELECT entitlement_id, entitlement_name, application, risk_score FROM entitlements WHERE risk_score >= 70 LIMIT 200",
  "tables": ["entitlements"],
  "row_count": 6,
  "rows": [ ... ],
  "generator": "LLM",
  "model": "gemini-2.5-flash"
}
```

The question is translated into a single `SELECT`, which is validated and then
run in a **`READ ONLY` transaction** against an allow-list of tables. The SQL and
the rows behind the answer come back on every response, including failures — an
answer nobody can check is not usable for governance.

> **This reports; it does not decide.** It reads current holdings and the
> outcomes of past analyses. Whether access *should* be granted is decided
> deterministically by the recommendation engine, which no model participates in.
> "Can X access Y?" returns what the records say, not a new access decision.

Four layers stand between a generated query and the database:

| Layer | Stops |
|---|---|
| `sqlglot` parse | Anything that is not one `SELECT`; statement stacking; `SELECT INTO`; `pg_read_file`, `dblink`, `pg_sleep` |
| Table allow-list | System catalogues, `alembic_version`, any table not explicitly readable |
| `READ ONLY` transaction | Every write — enforced by PostgreSQL, not by the parser |
| Row cap + statement timeout | Runaway or unbounded results |

The third is the real guarantee; the rest exist to give clear errors and keep
obviously wrong queries off the wire. `tests/unit/test_sql_guard.py` asserts 19
hostile queries are refused.

Requires a configured LLM (`503` otherwise) — set `CHAT_LLM_MODEL` to run chat on
a different model from the explanation layer, which keeps their quotas separate.

---

## MCP examples

```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("http://localhost:8000/mcp/") as client:
        print([t.name for t in await client.list_tools()])

        peers = (await client.call_tool(
            "find_peer_employees", {"employee_id": "EMP1002"}
        )).structured_content

        sod = (await client.call_tool(
            "check_sod_conflicts",
            {
                "employee_id": "EMP1002",
                "entitlement_ids": ["SAP_AP_CREATE_VENDOR", "SAP_AP_APPROVE_PAYMENT"],
            },
        )).structured_content
        print(sod["status"], sod["severity"], sod["conflicts"][0]["sod_id"])

asyncio.run(main())
```

```
['get_joiner', 'list_joiners', 'find_peer_employees', 'calculate_entitlement_affinity',
 'evaluate_entitlement_risk', 'validate_entitlement_policy', 'check_sod_conflicts',
 'generate_access_explanation', 'generate_sailpoint_request', 'run_access_analysis',
 'get_analysis_result', 'get_dashboard_metrics']
CONFLICT CRITICAL SOD-001
```

Full guide: **[docs/mcp.md](docs/mcp.md)**.

---

## LangGraph workflow

```
START → load_joiner → profile_joiner → find_peers → calculate_affinity
      → evaluate_risk → validate_policies → check_sod → make_decision
      → generate_explanation → generate_sailpoint_payload → persist_analysis → END
```

State is a `TypedDict` of validated Pydantic models. Data crossing the MCP
boundary arrives as JSON and is re-validated into the same model the service
produced, so a malformed response fails at the node that received it.

Eight of the eleven nodes reach their capability through an MCP tool call, over
a single MCP session per run. Two are deliberately in-process:

- **`make_decision`** — the governance kernel. Exposing it remotely would let a
  caller supply hand-made inputs and get an authoritative-looking verdict back.
- **`persist_analysis`** — the whole audit trail must land in one transaction.

**Failure policy.** Only `load_joiner` is fatal; without an identity there is
nothing to persist. Every other node records its error and lets the workflow
continue, so whatever was already established still reaches the database. This
is what guarantees that a failed explanation never makes a governance decision
disappear.

---

## Governance decision logic

### Peer analysis

Strategies are tried in order of decreasing precision, stopping at the first
that yields anyone. The strategy used is recorded on the analysis *and on every
recommendation* — a recommendation from a department-wide match is a weaker
claim than one from an exact role match, and the audit trail says which.

| Strategy | Base confidence |
|---|---|
| `job_role + department + job_level` | 0.95 |
| `job_role + department` | 0.85 |
| `department + job_level` | 0.70 |
| `department` | 0.55 |

```
confidence = base(strategy) × (0.6 + 0.4 × min(1, peer_count / saturation))
```

Only `ACTIVE` identities are ever peers. If no strategy matches, nothing is
recommended — unrelated employees are never silently substituted.

### Affinity

```
affinity = (peers holding the entitlement / total matched peers) × 100
```

For `EMP1001` against 8 matched peers:

| Entitlement | Peers | Affinity | Recommended (≥ 70%) |
|---|---|---|---|
| `SAP_FIN_DISPLAY` | 8/8 | 100.0% | yes |
| `POWERBI_FINANCE_VIEW` | 7/8 | 87.5% | yes |
| `SNOWFLAKE_FIN_READ` | 6/8 | 75.0% | yes |
| `JIRA_PROJECT_USER` | 5/8 | 62.5% | no |
| `SAP_FIN_POST_JOURNAL` | 2/8 | 25.0% | no |

### Risk

| Score | Band | Baseline requirement |
|---|---|---|
| 0–30 | `LOW` | Auto |
| 31–69 | `MEDIUM` | Auto unless policy or SoD says otherwise |
| 70–89 | `HIGH` | Manager approval |
| 90–100 | `CRITICAL` | Human review |

Risk is read from the entitlement catalogue. No model is asked what it thinks
the risk is.

### Policy

Six explicitly implemented types — `MUTUALLY_EXCLUSIVE_ENTITLEMENTS`,
`RISK_THRESHOLD_APPROVAL`, `EMPLOYMENT_TYPE_RESTRICTION`,
`LOCATION_RESTRICTION`, `JOB_LEVEL_RESTRICTION`, `DEPARTMENT_RESTRICTION` —
each a hand-written evaluator with a validated parameter model.

`rule_definition` JSONB supplies **parameters only**. It is never compiled,
`eval`-ed or otherwise executed, and unknown keys are rejected rather than
ignored. A policy that cannot be evaluated returns `ERROR`, which forces human
review — a broken control must never look like a satisfied one.

### Segregation of duties

Checks the requested set **plus what the identity already holds**, because the
most common real conflict is new access colliding with access granted months
earlier. Any conflict blocks both sides of the pair and routes to human review;
severity informs the reviewer but never downgrades the outcome.

### Decision precedence

```
affinity < threshold          → NOT_RECOMMENDED
SoD conflict                  → BLOCKED           (human review)
policy DENY                   → REJECTED          (no approval path)
policy BLOCK                  → BLOCKED           (human review)
policy ERROR                  → HUMAN_REVIEW      (failed closed)
risk CRITICAL                 → HUMAN_REVIEW
policy requires human review  → HUMAN_REVIEW
risk HIGH / policy → manager  → MANAGER_APPROVAL
otherwise                     → AUTO_APPROVED
```

`decide()` is a pure function — no I/O, no model calls, same inputs, same
output — and every recommendation carries a `decision_trace` recording which
rule produced which outcome.

### Explainability

Two artefacts per recommendation, both persisted: the **structured
explanation** (the evidence, as data) and the **narrative** (the same evidence
as prose). The structured form is built deterministically and is the only thing
a model ever sees.

The architecture does not depend on the model behaving: `generate_narrative`
returns a string, and every decision field is copied from the deterministic
result. A test feeds in a model that fabricates a contradictory verdict and
asserts the decision is unchanged.

#### Enabling a real model

The provider package is imported lazily, so only the one you name has to be
installed. For the Gemini API — which serves both the Gemini and the Gemma
families from a single key:

```bash
# .env
DEMO_MODE=false            # demo mode overrides everything and wins
LLM_PROVIDER=google        # `gemini` is accepted as an alias
LLM_MODEL=gemma-4-31b-it   # or gemini-2.5-pro, etc.
LLM_API_KEY=...
```

Gemma models reject the system role, so their prompt is folded into a single
user turn automatically — detected from the model id, no configuration needed.

Turning this on changes the prose and nothing else. With `LLM_PROVIDER=google`
the explanation is generated (`generator: LLM`); with `DEMO_MODE=true` it comes
from the template (`generator: DETERMINISTIC`); the decisions, affinity scores,
risk scores and approval tiers are byte-identical either way.

---

## SailPoint simulation

**No SailPoint environment is contacted, and nothing is provisioned.**

`SailPointService.generate_request_payload()` builds the payload an IdentityIQ
connector would submit, marks it `SIMULATED`, and persists it to
`sailpoint_requests`.

Only entitlements whose decision meets the configured approval criteria
(`SAILPOINT_INCLUDED_STATUSES`, default `AUTO_APPROVED,MANAGER_APPROVAL`) are
included. Blocked, rejected, review-pending and not-recommended entitlements
appear under `excluded_entitlements` **with their reason**, so the exclusion is
auditable rather than invisible.

```json
{
  "identity": "EMP1001",
  "request_type": "GrantAccess",
  "requested_entitlements": [
    {
      "application": "SAP",
      "entitlement": "SAP_FIN_DISPLAY",
      "entitlement_name": "SAP Finance Display",
      "operation": "Add",
      "approval_tier": "AUTO",
      "risk_level": "LOW",
      "affinity_score": 100.0
    }
  ],
  "justification": "Onboarding access for Jane Smith (EMP1001), Financial Analyst in Finance. Recommended based on peer analysis and validated by risk, policy and segregation-of-duties controls.",
  "source": "Agentic Access Recommendation Engine",
  "status": "SIMULATED",
  "metadata": { "simulated": true, "analysis_id": "..." },
  "excluded_entitlements": [
    { "entitlement": "SAP_AP_APPROVE_PAYMENT", "recommendation_status": "BLOCKED", "reason": "..." }
  ]
}
```

`submit_request()` raises `NotImplementedError` rather than returning a fake
success — a stub that lies about provisioning is worse than no stub. It is the
seam a real connector drops into.

---

## Testing

```bash
pytest                              # everything
pytest tests/unit                   # pure, no database
pytest tests/integration            # against a real PostgreSQL
pytest tests/test_mcp.py            # MCP protocol
pytest -m "not slow"                # skip the subprocess transport test
```

Integration tests need PostgreSQL. Point `TEST_DATABASE_URL` at a disposable
database, or let it derive a `newjoiner_test` sibling from your `POSTGRES_*`
settings. They skip cleanly if nothing is reachable.

```bash
TEST_DATABASE_URL="postgresql+psycopg://postgres:...@127.0.0.1:55432/newjoiner_test" pytest
```

There is **no SQLite fallback in the tests**. The schema uses JSONB, native
UUID and `ON CONFLICT`; a substitute engine would not exercise them faithfully.

The suite forces `DEMO_MODE=true` for its whole run, so no test ever reaches a
real model — the governance workflow has to be provable without one.

**236 tests** — 120 unit, 100 integration, 16 MCP — covering:

| Area | What is asserted |
|---|---|
| **Client agreement** | All 13 rows of the client's own `peer_affinity_scores` reproduce from `identities` |
| Peer analysis | Exact match, each fallback level, no peers, leaver/joiner exclusion |
| Affinity | 5/5=100, 4/5=80, 2/3=66.67, 1/5=20, zero-peer division, threshold inclusivity |
| Risk | Every band and every boundary value, configurability |
| Policy | Pass, block, deny, disabled policies, unknown type and corrupt definition failing closed |
| SoD | No conflict, conflict, severity handling, conflict against existing access, disabled rules |
| Decision | Every precedence branch, plus determinism under repetition |
| Explainability | Required fields, LLM failure fallback, **prose cannot change a decision** |
| SailPoint | Approved included, blocked excluded, payload structure, `SIMULATED`, submit unimplemented |
| MCP | Discovery, schemas, invocation, error propagation, stdio subprocess transport |
| LangGraph | Node sequence, exact set of MCP tools used, persistence, failure isolation |
| REST | Every endpoint, error codes, OpenAPI completeness, request size limit |
| **Chat safety** | 19 hostile queries refused; row cap enforced; a smuggled `DELETE` refused by PostgreSQL itself |

---

## Docker

```bash
docker compose up --build          # postgres → migrate → seed → api
docker compose --profile mcp up    # also a standalone MCP server on :8081
```

Build and run the image alone:

```bash
docker build -t newjoiner:0.1.0 .
docker run --rm -p 8000:8000 --env-file .env newjoiner:0.1.0
```

The entrypoint supports `api` (default), `mcp`, `migrate`, `seed` and `demo`.
The image runs as a non-root user and carries a healthcheck against
`/api/v1/health`.

**Migrations are opt-in** (`RUN_MIGRATIONS=true`). They default to off because
with several replicas every one would race to migrate on boot; run `migrate` as
a discrete step instead.

Docker Compose is for local development only. In production the database is the
Space-Cloud PostgreSQL add-on.

---

## Space-Cloud deployment

Space Cloud is the target runtime. PostgreSQL is external and provided by the
Space-Cloud add-on; the application never provisions a database.

```bash
space-cli add database postgres --name postgres
space-cli apply -f deploy/space-cloud-service.yaml --project newjoiner
space-cli apply -f deploy/space-cloud-ingress.yaml --project newjoiner
```

Or apply the plain Kubernetes manifests, which run migrations as a first-class
Job:

```bash
kubectl apply -f deploy/kubernetes.yaml
kubectl -n newjoiner wait --for=condition=complete job/newjoiner-migrate --timeout=180s
kubectl -n newjoiner rollout status deploy/newjoiner-api
```

Full instructions — every environment variable, the migration and seed
processes, health checks and how to reach the in-cluster database from a
workstation: **[docs/space-cloud-deployment.md](docs/space-cloud-deployment.md)**.

---

## Project structure

```
app/
├── main.py                  FastAPI app, middleware, error handlers, MCP mount
├── config.py                Pydantic Settings (environment-driven)
├── logging.py               structlog + correlation context
├── api/routes/              joiners · analyses · access_requests · dashboard · health
├── agents/
│   ├── graph.py             the LangGraph workflow
│   ├── state.py             typed state
│   ├── mcp_bridge.py        sync → async MCP client bridge (4 modes)
│   └── nodes/               one module per workflow step
├── mcp/
│   ├── server.py            FastMCP server
│   └── tools/               tool handlers (shared with `direct` mode)
├── domain/
│   ├── enums.py             the governance vocabulary
│   ├── models.py            typed domain models
│   ├── exceptions.py        domain errors + MCP wire-format recovery
│   └── rules/policy_rules.py   explicit policy evaluators
├── services/                ALL governance logic lives here
├── db/
│   ├── models/              SQLAlchemy ORM
│   ├── repositories/        every SQL statement
│   └── session.py           engine and session management
└── schemas/api.py           HTTP request/response contracts

alembic/                     migrations
seed/                        client extract: client/*.csv + *.json ground truth
scripts/                     convert_client_csv.py · seed_database.py · run_demo.py
tests/                       unit · integration · test_mcp.py
deploy/                      Space Cloud + Kubernetes manifests
docs/                        architecture.md · mcp.md · space-cloud-deployment.md
```

---

## Design notes

Decisions worth knowing about, and why:

- **`recommendations.analysis_id`** — the original specification wrote this
  column as `analysis_idx`. It is a foreign key to
  `joiner_analyses.analysis_id`, so it carries that name.
- **`employees.employment_type`** — added alongside `employment_status`. Status
  is a lifecycle state; type is the contract form. The contractor policy needs
  the latter, and overloading the former would conflate "is this person
  employed?" with "how are they employed?".
- **`recommendation_explanations`** — a table the original schema did not list,
  added because persisting *both* the structured explanation and the narrative
  is what makes the prose auditable against the facts behind it.
- **snake_case throughout the API.** The specification's JSON examples mix
  conventions; snake_case is the dominant one and is used consistently.
- **`NOT_EVALUATED`** appears as a policy/SoD status for below-threshold
  candidates. Those controls genuinely were not run — there was no proposal to
  govern — and recording `PASS` would misrepresent the audit trail.
- **In-memory MCP is the default transport** for the API path. It is the real
  protocol, without paying for a subprocess and a second connection pool on
  every analysis; the stdio path is covered by the demo and its own test.

---

## Future enhancements

- **Real SailPoint connector** behind the existing `submit_request()` seam:
  authentication, the access-request API call, idempotency, and reconciliation
  of approval outcomes back onto the recommendation rows.
- **Authentication and authorisation** on the API and the MCP surface (out of
  scope for this MVP by design).
- **Role mining** — cluster peer entitlement sets into candidate business roles
  rather than recommending entitlement by entitlement.
- **Time-bounded access** and automatic recertification scheduling.
- **Mover and leaver flows** reusing the same engines: what to revoke on a role
  change, what to remove on exit.
- **Policy simulation** — "what would change if this threshold moved?" against
  historical analyses.
- **Feedback loop** — track approver overrides and surface entitlements whose
  recommendations are routinely reversed.
