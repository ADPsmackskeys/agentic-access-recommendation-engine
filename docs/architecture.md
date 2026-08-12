# Architecture

## 1. What this system is

An AI-assisted identity governance service that answers one question for a new
joiner: **which application entitlements should this person receive?**

It answers it by looking at what comparable employees already hold, then
subjecting every candidate entitlement to the same controls a human access
reviewer would apply — risk banding, policy validation and segregation of
duties — before producing an explainable decision and a SailPoint
IdentityIQ-style access request.

The central design commitment is stated once here and enforced everywhere
below:

> **The LLM never decides anything.** Affinity, risk classification, policy
> outcomes, SoD detection, approval tier and the final recommendation status
> are computed by deterministic rules over database state. The model layer
> converts that structured evidence into prose. Nothing else.

---

## 2. System architecture

```mermaid
flowchart TD
    CLIENT[REST client] --> API[FastAPI<br/>app/main.py]
    AGENT[MCP client<br/>agent runtime, Claude Desktop, CLI] --> MCPS

    API --> GRAPH[LangGraph workflow<br/>app/agents/graph.py]
    API -. mounts .-> MCPS[FastMCP server<br/>app/mcp/server.py]

    GRAPH --> BRIDGE[MCP client bridge<br/>app/agents/mcp_bridge.py]
    BRIDGE -->|MCP protocol| MCPS
    MCPS --> TOOLS[MCP tools<br/>app/mcp/tools/]
    TOOLS --> SERVICES

    GRAPH -->|in-process| KERNEL[Decision engine<br/>app/services/decision_service.py]
    GRAPH -->|in-process| PERSIST[Persistence node]

    SERVICES[Domain services<br/>app/services/] --> REPOS[Repositories<br/>app/db/repositories/]
    KERNEL --> SERVICES
    PERSIST --> REPOS
    REPOS --> DB[(PostgreSQL<br/>Space-Cloud)]

    SERVICES --> LLM[LLM abstraction<br/>app/services/llm_service.py]
    SERVICES --> SP[SailPoint payload generator<br/>simulated]

    style KERNEL fill:#f9e79f,stroke:#b7950b
    style DB fill:#d4e6f1,stroke:#2874a6
    style LLM fill:#fadbd8,stroke:#b03a2e
```

---

## 3. Component responsibilities

| Layer | Location | Responsibility | Must not |
|---|---|---|---|
| REST API | `app/api/`, `app/main.py` | HTTP contract, validation, error mapping, correlation ids | Contain business rules |
| MCP server | `app/mcp/` | Expose domain capabilities as MCP tools | Contain business rules |
| Workflow | `app/agents/` | Sequence the analysis, thread typed state, call MCP tools | Talk HTTP; write SQL |
| Domain services | `app/services/` | **All governance logic** | Import FastAPI or LangGraph |
| Domain models/rules | `app/domain/` | Vocabulary, typed models, policy evaluators | Touch the database |
| Repositories | `app/db/repositories/` | Every SQL statement in the system | Make decisions |
| Config/logging | `app/config.py`, `app/logging.py` | Environment-driven settings, structured logs | Hold secrets in source |

The rule that keeps this honest: **REST and MCP are two transports over one
service layer.** `app/api/routes/joiners.py` and `app/mcp/tools/identity_tools.py`
both call `PeerAnalysisService`. Neither reimplements it.

---

## 4. The analysis workflow

```mermaid
flowchart TD
    START([START]) --> A[load_joiner]
    A --> B[profile_joiner]
    B --> C[find_peers]
    C --> D[calculate_affinity]
    D --> E[evaluate_risk]
    E --> F[validate_policies]
    F --> G[check_sod]
    G --> H[make_decision]
    H --> I[generate_explanation]
    I --> J[generate_sailpoint_payload]
    J --> K[persist_analysis]
    K --> END([END])

    style H fill:#f9e79f,stroke:#b7950b
    style K fill:#d5f5e3,stroke:#1e8449
```

Yellow = the governance kernel (in-process). Green = the single persistence
transaction. Every other node reaches its capability through an MCP tool call.

The graph is linear because the governance sequence is linear: affinity needs
peers, and the decision needs risk, policy and SoD to have all reported.

### State

`app/agents/state.py` defines `AccessRecommendationState` as a `TypedDict` of
**validated Pydantic models**, not loose dictionaries. Data returned across the
MCP boundary arrives as JSON and is re-validated into the same model the
service produced, so a malformed tool response fails at the node that received
it rather than three nodes later.

`errors`, `steps_completed` and `mcp_tool_calls` use an append reducer, so a
late failure never erases the record of an earlier one.

### Failure policy

| Node | On failure |
|---|---|
| `load_joiner` | Fatal. Without an identity there is nothing to analyse and nothing that could be persisted against a foreign key. |
| every other node | Records the error on state and continues, so everything already established still reaches the database. |

This is what delivers the requirement that **a failed explanation must not make
the governance decision disappear**. It is enforced at two levels: the
explanation service falls back to a deterministic template when the model
fails, and the node wrapper tolerates an outright crash of the whole step.
`tests/integration/test_workflow.py::test_explanation_failure_does_not_lose_the_decisions`
asserts both.

---

## 5. MCP architecture

```mermaid
flowchart LR
    subgraph Workflow process
        NODE[LangGraph node] --> INV[McpToolInvoker]
        INV --> LOOP[private event loop<br/>background thread]
    end

    LOOP -->|inmemory: memory streams| SRV
    LOOP -->|stdio: subprocess| SUB[python -m app.mcp.server]
    LOOP -->|http: Streamable HTTP| REMOTE[FastAPI /mcp]

    SUB --> SRV
    REMOTE --> SRV
    SRV[FastMCP server] --> H[tool handlers]
    H --> S[domain services] --> P[(PostgreSQL)]
```

### Why a bridge exists

The workflow nodes are synchronous — they ultimately hit a synchronous
SQLAlchemy session — while the MCP client is asynchronous.
`app/agents/mcp_bridge.py` owns that boundary: it runs a private event loop on
a background thread and keeps **one MCP session open for the whole workflow
run**, so an eleven-node analysis performs one protocol handshake rather than
eight.

### The four modes, and the tradeoff

| Mode | Transport | Used by |
|---|---|---|
| `inmemory` *(default)* | Real MCP client/server session over in-process memory streams | REST API path |
| `stdio` | Real MCP client spawning `python -m app.mcp.server` as a subprocess | Demo script, MCP integration tests |
| `http` | Real MCP client against a running Streamable-HTTP server | Distributed deployments |
| `direct` | No MCP; calls the tool handler in process | `run_access_analysis` only |

**The tradeoff, stated plainly.** `inmemory` is the default for the API path
because spawning a subprocess — and with it a second database connection pool —
on every analysis costs seconds and buys nothing when the tools live in the
same deployable. It is still the MCP protocol end to end: the client
initialises a session, negotiates capabilities and exchanges JSON-RPC messages;
it simply does so without a socket. `stdio` exercises the out-of-process path
and is what the demo and `tests/test_mcp.py::test_stdio_transport_works` use,
so the cross-process contract is covered rather than assumed.

`direct` exists for exactly one reason: `run_access_analysis` is itself an MCP
tool, and a tool handler must not re-enter the transport currently serving it.

### One handler, two transports

Tool handlers are **module-level functions**, not closures built inside a
`register()` call:

```python
# app/mcp/tools/identity_tools.py
@mcp_tool_handler
def find_peer_employees(employee_id: str) -> PeerAnalysisResult:
    with tool_session() as session:
        return PeerAnalysisService(session).find_peers(employee_id)

HANDLERS = {"find_peer_employees": find_peer_employees, ...}
```

`app/mcp/tools/__init__.py` merges every module's `HANDLERS` into
`ALL_HANDLERS`. The MCP server wraps them in the protocol; `direct` mode calls
the identical function object. That is what makes "MCP tools are thin wrappers
over the domain services" checkable rather than aspirational.

### Errors across the boundary

MCP flattens exceptions to strings. The handler prefixes every domain failure
with its code (`"employee_not_found: ..."`), and `_translate_error` in the
bridge rebuilds the original exception type on the client side. Without this, a
missing employee reaching the API through the workflow would surface as a
gateway error instead of a 404 — `tests/integration/test_api.py::test_analyzing_an_unknown_joiner_returns_404`
pins that behaviour.

---

## 6. Data model

```mermaid
erDiagram
    employees ||--o{ employee_entitlements : holds
    entitlements ||--o{ employee_entitlements : granted_as
    entitlements ||--o{ sod_rules : "referenced by"
    employees ||--o{ joiner_analyses : "analysed in"
    joiner_analyses ||--o{ recommendations : produces
    joiner_analyses ||--o{ sailpoint_requests : generates
    recommendations ||--o{ recommendation_evidence : "supported by"
    recommendations ||--o{ policy_results : "validated by"
    recommendations ||--o{ sod_results : "checked by"
    recommendations ||--o| recommendation_explanations : "explained by"

    employees {
        string employee_id PK
        string department
        string job_role
        string job_level
        string location
        string employment_status
        string employment_type
    }
    entitlements {
        string entitlement_id PK
        string application
        int risk_score
        string risk_category
    }
    policies {
        string policy_id PK
        string policy_type
        jsonb rule_definition
        bool enabled
    }
    sod_rules {
        string sod_id PK
        string entitlement_1 FK
        string entitlement_2 FK
        string severity
        bool enabled
    }
    joiner_analyses {
        uuid analysis_id PK
        string employee_id FK
        string matching_strategy
        float peer_confidence
        jsonb peer_ids
    }
    recommendations {
        uuid recommendation_id PK
        uuid analysis_id FK
        float affinity_score
        int risk_score
        string policy_status
        string sod_status
        string recommendation_status
        string approval_tier
        jsonb decision_trace
    }
```

### Deliberate schema choices

- **`recommendations.analysis_id`** — the original specification wrote this
  column as `analysis_idx`. It is a foreign key to
  `joiner_analyses.analysis_id`, so it carries that name here.
- **`employees.employment_type`** — added alongside `employment_status`. Status
  is a lifecycle state (`ACTIVE`, `PENDING_START`, `TERMINATED`); type is the
  contract form (`EMPLOYEE`, `CONTRACTOR`). The contractor restriction policy
  needs the latter, and overloading the former would have made "is this person
  employed?" and "how are they employed?" the same column.
- **`recommendation_explanations`** — a table the original schema did not list.
  The requirement to persist *both* the structured explanation and the
  generated narrative needs somewhere to put them, and keeping them together is
  what makes the prose auditable: you can always check it against the facts it
  was generated from.
- **JSONB is used for** policy rule parameters, decision traces, peer id lists,
  structured explanations and SailPoint payloads — all ordered or
  schema-flexible blobs read back whole. Everything queried or aggregated
  (statuses, scores, tiers, strategies) is a real column with an index.

---

## 7. Decision flow

```mermaid
flowchart TD
    START([candidate entitlement]) --> AFF{affinity >= threshold?}
    AFF -->|no| NR[NOT_RECOMMENDED]
    AFF -->|yes| SOD{SoD conflict?}
    SOD -->|yes| BL1[BLOCKED · human review]
    SOD -->|no| POL{policy outcome}
    POL -->|DENY| REJ[REJECTED · no approval path]
    POL -->|BLOCK| BL2[BLOCKED · human review]
    POL -->|ERROR| HR1[HUMAN_REVIEW · failed closed]
    POL -->|PASS / REQUIRES_APPROVAL| RISK{risk band}
    RISK -->|CRITICAL| HR2[HUMAN_REVIEW]
    RISK -->|other| PT{policy demands human review?}
    PT -->|yes| HR3[HUMAN_REVIEW]
    PT -->|no| HIGH{HIGH risk or policy wants manager?}
    HIGH -->|yes| MA[MANAGER_APPROVAL]
    HIGH -->|no| AA[AUTO_APPROVED]

    style NR fill:#eaeded
    style BL1 fill:#f5b7b1
    style BL2 fill:#f5b7b1
    style REJ fill:#e6b0aa
    style HR1 fill:#fad7a0
    style HR2 fill:#fad7a0
    style HR3 fill:#fad7a0
    style MA fill:#d6eaf8
    style AA fill:#d5f5e3
```

Precedence is the point. Reading it in order:

1. **Affinity gates everything.** Below threshold, nothing else is even
   evaluated — there is no proposal to govern.
2. **SoD outranks policy and risk.** A toxic combination is a structural
   problem with the *set* of access, not a property of one entitlement.
3. **DENY is terminal; BLOCK is reviewable.** A contractor barred from
   sensitive data is `REJECTED` with no approval path. A policy block goes to
   `HUMAN_REVIEW` where a person can weigh it.
4. **ERROR fails closed.** A control that could not be evaluated becomes
   `HUMAN_REVIEW`, never a silent pass. A broken control must not look like a
   satisfied one.

`decide()` is a pure function: no I/O, no model calls, same inputs → same
output. `tests/unit/test_decision.py` states each of these rules as an
assertion.

### Where each input comes from

| Input | Source | Determinism |
|---|---|---|
| Affinity | `(peers holding / total peers) × 100` | Recomputable by counting rows |
| Risk | `entitlements.risk_score` → configured bands | Catalogue lookup |
| Policy | Registered evaluators over `rule_definition` parameters | No expression evaluation anywhere |
| SoD | Enabled `sod_rules` against requested ∪ existing access | Set intersection |

---

## 8. Policy engine

There is no generic rule interpreter. Each `policy_type` maps to a hand-written
evaluator with a Pydantic parameter model:

```mermaid
flowchart LR
    ROW[(policies row)] --> TYPE{policy_type}
    TYPE --> EV[registered evaluator]
    DEF[rule_definition JSONB] --> PARAMS[Pydantic params<br/>extra = forbid]
    PARAMS --> EV
    CTX[PolicyContext<br/>identity + requested + existing + risk] --> EV
    EV --> OUT[RuleOutcome<br/>status + tier + reason]
    TYPE -->|unknown| ERR[InvalidPolicyDefinitionError]
    PARAMS -->|invalid| ERR
    ERR --> FC[status = ERROR<br/>fail closed]
```

`rule_definition` supplies **parameters only**. It is never compiled, `eval`-ed
or otherwise executed, and `extra="forbid"` means an unrecognised key in a rule
definition is rejected rather than ignored. Adding a policy type means writing
an evaluator and registering it — deliberate friction, because a governance
control should be reviewable code rather than a string in a database.

Supported types: `MUTUALLY_EXCLUSIVE_ENTITLEMENTS`, `RISK_THRESHOLD_APPROVAL`,
`EMPLOYMENT_TYPE_RESTRICTION`, `LOCATION_RESTRICTION`, `JOB_LEVEL_RESTRICTION`,
`DEPARTMENT_RESTRICTION`.

---

## 9. Peer analysis

Strategies are tried in order of decreasing precision, stopping at the first
that yields anyone:

```
job_role + department + job_level   base confidence 0.95
        ↓ (none found)
job_role + department               base confidence 0.85
        ↓
department + job_level              base confidence 0.70
        ↓
department                          base confidence 0.55
        ↓
NONE — no recommendations from peer evidence
```

The strategy that produced the group is recorded on the analysis *and on every
recommendation*, because a recommendation derived from a department-wide match
is a much weaker claim than one from an exact role match, and the audit trail
has to say which it was.

```
confidence = base(strategy) × (0.6 + 0.4 × min(1, peer_count / saturation))
```

Group size matters as well as precision: a single peer is evidence of almost
nothing, and beyond the saturation point (default 8) more peers add little.

Only `ACTIVE` identities are ever selected. Leavers and not-yet-started joiners
are excluded — the seed data deliberately gives two terminated employees toxic
privileged access, so any regression in that filter surfaces immediately as
candidate entitlements.

When no strategy matches, the result says so and no entitlements are
recommended. Unrelated employees are never silently substituted.

---

## 10. Explainability

Two artefacts per recommendation, both persisted:

```mermaid
flowchart LR
    DEC[AccessDecision<br/>deterministic] --> STRUCT[StructuredExplanation<br/>evidence as data]
    STRUCT --> TPL[template narrative]
    STRUCT -->|only input| LLM{LLM available?}
    LLM -->|yes| GEN[model narrative]
    LLM -->|no / failure| TPL
    GEN --> STORE[(recommendation_explanations)]
    TPL --> STORE
    STRUCT --> STORE
```

The structured form is built deterministically from the decision record and is
the **only** thing the model ever sees. The narrative is prose about a decision
that has already been made.

The system prompt forbids introducing facts, but the architecture does not rely
on the model obeying it: `generate_narrative` returns a *string*, and every
decision field on the stored explanation is copied from the deterministic
result. `tests/unit/test_explanation.py::test_llm_prose_cannot_change_the_decision`
feeds in a model that fabricates a contradictory verdict and asserts the
decision is unchanged.

---

## 11. SailPoint integration (simulated)

**No SailPoint environment is contacted.** `SailPointService` builds the payload
an IdentityIQ connector would submit, marks it `SIMULATED`, and persists it.

```mermaid
flowchart LR
    DECISIONS[decisions] --> FILTER{status in<br/>SAILPOINT_INCLUDED_STATUSES?}
    FILTER -->|yes| INC[requested_entitlements]
    FILTER -->|no| EXC[excluded_entitlements<br/>with reason]
    INC --> PAYLOAD[SailPointRequestPayload<br/>status = SIMULATED]
    EXC --> PAYLOAD
    PAYLOAD --> DB[(sailpoint_requests)]
    PAYLOAD -.future.-> CONNECTOR[submit_request<br/>NotImplementedError]
```

Default inclusion is `AUTO_APPROVED` and `MANAGER_APPROVAL` — items SailPoint
can route for approval. Blocked, rejected, review-pending and not-recommended
entitlements are listed under `excluded_entitlements` **with their reason**, so
the exclusion is auditable rather than invisible.

`submit_request()` raises `NotImplementedError` rather than returning a fake
success. A stub that lies about provisioning is worse than no stub. It is the
seam a real connector drops into.

---

## 12. Observability

Every log line carries `correlation_id`, `analysis_id`, `employee_id` and
`workflow_step`, merged automatically from context variables so no call site
has to remember to pass them. Logged events include workflow start/end, each
step boundary, every MCP tool invocation, peer matching, affinity, risk, policy,
SoD, the final decision, payload generation and persistence.

`_redact_processor` blanks anything key-named like a secret, and the database
URL is redacted at source by `Settings.safe_database_url()`. Logs go to
**stderr**, which is what keeps the stdio MCP transport usable — stdout there
is the JSON-RPC channel.

---

## 13. Future SailPoint integration

```mermaid
flowchart TD
    subgraph Today
        A1[generate_request_payload] --> A2[(sailpoint_requests<br/>status SIMULATED)]
    end
    subgraph Next
        B1[generate_request_payload] --> B2[submit_request]
        B2 --> B3[IdentityIQ access-request API]
        B3 --> B4[(status SUBMITTED<br/>+ external request id)]
        B4 --> B5[poll / webhook:<br/>approval outcome]
    end
    A1 -.same payload.-> B1
```

What is already in place for it: the `SailPointRequestStatus` enum carries
`SUBMITTED` and `FAILED`; `sailpoint_requests` stores the payload as JSONB
alongside a status column; and `submit_request()` exists as an explicit seam.
What a real connector adds is authentication, the HTTP call, retry/idempotency
handling, and reconciliation of approval outcomes back onto the recommendation
rows.

---

## 14. Deployment

```mermaid
flowchart TD
    subgraph SC[Space-Cloud Kubernetes]
        GW[Space Cloud gateway] --> API[newjoiner-api pods]
        API --> PG[(postgres.db.svc.cluster.local:5432<br/>Space-Cloud PostgreSQL add-on)]
        JOB[newjoiner-migrate Job<br/>alembic upgrade head + seed] --> PG
    end
    AGENT[External MCP client] -->|/mcp| GW
    REST[REST client] -->|/api/v1| GW
```

Migrations run as a one-shot Job rather than on pod start: with more than one
replica, every replica would race to migrate. `RUN_MIGRATIONS` defaults to
`false` for exactly that reason.

Configuration is entirely environment-driven, so the same image runs against a
local PostgreSQL container and the Space-Cloud add-on with no code change. See
[`space-cloud-deployment.md`](space-cloud-deployment.md).
