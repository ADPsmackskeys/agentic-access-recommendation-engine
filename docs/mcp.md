# MCP guide

This service exposes its identity-governance capabilities as **MCP tools**, and
the LangGraph workflow reaches those capabilities by acting as an **MCP
client**. This document covers how to start the server, what it exposes, and
how to discover and invoke the tools.

---

## 1. Starting the server

### Option A — mounted on the API (no extra process)

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

MCP is served over Streamable HTTP at:

```
http://localhost:8000/mcp/
```

The FastMCP ASGI app's lifespan is chained into the FastAPI lifespan, which is
what starts the Streamable-HTTP session manager. (Mounting without that chaining
yields a 500 on the first MCP request — a real bug fixed during this build.)

### Option B — standalone, stdio

The transport an external MCP client (an agent runtime, Claude Desktop, an IDE
plugin) uses when it launches the server itself:

```bash
python -m app.mcp.server --transport stdio
```

Logs go to **stderr**; stdout is the JSON-RPC channel.

### Option C — standalone, Streamable HTTP

```bash
python -m app.mcp.server --transport http --host 0.0.0.0 --port 8081
# or: MCP_TRANSPORT=http python -m app.mcp.server
```

### Client configuration

```json
{
  "mcpServers": {
    "agentic-access-recommendation-engine": {
      "command": "python",
      "args": ["-m", "app.mcp.server", "--transport", "stdio"],
      "cwd": "/path/to/agentic-access-recommendation-engine",
      "env": {
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "...",
        "POSTGRES_DB": "newjoiner",
        "DEMO_MODE": "true"
      }
    }
  }
}
```

---

## 2. Tools exposed

| Tool | Purpose |
|---|---|
| `get_joiner` | Identity profile including current entitlement holdings |
| `list_joiners` | Identities awaiting onboarding (`PENDING_START`) |
| `find_peer_employees` | Peer group, matching strategy used, confidence |
| `calculate_entitlement_affinity` | Per-entitlement affinity with peer evidence |
| `evaluate_entitlement_risk` | Risk band and baseline approval tier |
| `validate_entitlement_policy` | Policy outcomes per entitlement |
| `check_sod_conflicts` | Toxic-combination detection with severity |
| `generate_access_explanation` | Structured evidence + narrative |
| `generate_sailpoint_request` | Simulated IdentityIQ request payload |
| `run_access_analysis` | The complete LangGraph workflow |
| `get_analysis_result` | Retrieve a persisted analysis |
| `get_dashboard_metrics` | Governance summary metrics |

Tools carry tags (`identity`, `peer-analysis`, `affinity`, `risk`, `policy`,
`sod`, `explainability`, `sailpoint`, `workflow`, `reporting`, `read`) for
clients that filter on them.

---

## 3. Discovery

```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("http://localhost:8000/mcp/") as client:
        for tool in await client.list_tools():
            print(f"{tool.name:34s} {tool.description[:70]}")

asyncio.run(main())
```

```
get_joiner                         Fetch the identity profile of an employee, including their curr
list_joiners                       List identities awaiting onboarding (employment status PENDING_
find_peer_employees                Find the peer group whose access predicts what this identity ne
calculate_entitlement_affinity     Calculate, for every entitlement held by the given peer group,
evaluate_entitlement_risk          Classify entitlements against the configured risk bands (0-30 L
validate_entitlement_policy        Run every enabled governance policy against the requested entit
check_sod_conflicts                Check the requested entitlements, combined with the identity's
generate_access_explanation        Produce the structured evidence bundle and the natural-language
generate_sailpoint_request         Build a SailPoint IdentityIQ-style access-request payload from
run_access_analysis                Run the complete onboarding analysis for a new joiner: profile
get_analysis_result                Retrieve a previously persisted analysis by id, including recom
get_dashboard_metrics              Summary governance metrics across all analyses: joiner and anal
```

Input schemas are generated from the handlers' type hints:

```python
tools = {t.name: t for t in await client.list_tools()}
print(tools["check_sod_conflicts"].inputSchema)
```

```json
{
  "type": "object",
  "properties": {
    "employee_id": {"type": "string"},
    "entitlement_ids": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["employee_id", "entitlement_ids"],
  "additionalProperties": false
}
```

---

## 4. Invocation

### A full governance sequence

```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("http://localhost:8000/mcp/") as client:
        peers = (await client.call_tool(
            "find_peer_employees", {"employee_id": "NJ1001"}
        )).structured_content
        print(peers["matching_strategy"], peers["peer_count"], peers["confidence"])

        affinity = (await client.call_tool(
            "calculate_entitlement_affinity",
            {
                "employee_id": "NJ1001",
                "peer_ids": peers["peer_ids"],
                "matching_strategy": peers["matching_strategy"],
            },
        )).structured_content

        requested = [
            c["entitlement_id"] for c in affinity["candidates"] if c["meets_threshold"]
        ]

        sod = (await client.call_tool(
            "check_sod_conflicts",
            {"employee_id": "NJ1001", "entitlement_ids": requested},
        )).structured_content
        print(sod["status"], sod["severity"])
        for conflict in sod["conflicts"]:
            print(conflict["sod_id"], conflict["entitlement_1"], "+", conflict["entitlement_2"])

asyncio.run(main())
```

```
job_role_department_job_level 6 0.855
CONFLICT CRITICAL
SOD001 SAP_VENDOR_CREATE + SAP_PAYMENT_APPROVER
```

### The whole workflow in one call

```python
result = (await client.call_tool(
    "run_access_analysis", {"employee_id": "NJ1001"}
)).structured_content

print(result["analysis_id"], result["status"], len(result["decisions"]))
```

### Errors

Domain failures arrive as MCP tool errors carrying the domain error code:

```python
from fastmcp.exceptions import ToolError

try:
    await client.call_tool("get_joiner", {"employee_id": "NOPE"})
except ToolError as exc:
    print(exc)   # employee_not_found: Employee 'NOPE' does not exist.
```

That code prefix is the wire format for domain errors: the client bridge parses
it back into the original exception type, which is how a missing employee still
produces a 404 from the REST API rather than a gateway error.

---

## 5. How the workflow uses MCP

```
LangGraph node → McpToolInvoker → MCP session → FastMCP server → domain service → PostgreSQL
```

Eight of the eleven workflow nodes reach their capability through an MCP tool
call, over a single MCP session opened for the whole run:

| Node | Tool |
|---|---|
| `load_joiner` | `get_joiner` |
| `find_peers` | `find_peer_employees` |
| `calculate_affinity` | `calculate_entitlement_affinity` |
| `evaluate_risk` | `evaluate_entitlement_risk` |
| `validate_policies` | `validate_entitlement_policy` |
| `check_sod` | `check_sod_conflicts` |
| `generate_explanation` | `generate_access_explanation` |
| `generate_sailpoint_payload` | `generate_sailpoint_request` |

The exceptions are deliberate and documented in their own modules:

- **`make_decision`** runs in process. The decision engine is the governance
  kernel — the one component whose output *is* the authorisation outcome.
  Exposing it remotely would create a seam where a caller could supply
  hand-made affinity, risk, policy and SoD inputs and get an
  authoritative-looking verdict back.
- **`persist_analysis`** runs in process. The whole audit trail has to land in
  one database transaction, and a tool call that opens its own session cannot
  join the caller's.

`tests/integration/test_workflow.py::test_workflow_reaches_its_capabilities_over_mcp`
asserts the exact set of tools the workflow invokes, so this cannot quietly
drift.

### Execution modes

Set with `MCP_CLIENT_MODE`:

| Mode | What happens |
|---|---|
| `inmemory` *(default)* | Real MCP session over in-process memory streams |
| `stdio` | Real MCP client spawning `python -m app.mcp.server` |
| `http` | Real MCP client against a running Streamable-HTTP server |
| `direct` | No MCP; the same handler called in process |

See [architecture.md](architecture.md#5-mcp-architecture) for the tradeoff
behind the default.

---

## 6. Proving MCP is really in use

```bash
# 1. Tool discovery and invocation over a real MCP session
pytest tests/test_mcp.py -v

# 2. The out-of-process stdio transport specifically
pytest tests/test_mcp.py::test_stdio_transport_works -v

# 3. Watch the workflow make its MCP calls
python scripts/run_demo.py --employee NJ1007

# 4. Run the workflow itself over the subprocess transport
python scripts/run_demo.py --mcp-mode stdio --skip-mcp-demo
```

The structured logs emit `mcp.call`, `mcp.tool.invoked` and
`mcp.tool.completed` for every invocation, tagged with the analysis correlation
id.
