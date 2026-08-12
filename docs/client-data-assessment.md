# Client data assessment

Analysis of the seven client CSV extracts against the built system. Unlike the
earlier [schema RCA](schema-rca.md), which reasoned from table names alone, this
is based on the actual data: it has been loaded and the engine has been run
against it.

**Verdict: the structure does not need to change.** One nullable column was
added to load the extracts. The governance engine reproduces the client's own
affinity figures exactly, with no changes to any logic.

---

## 1. Evidence

The client's extract is now the project's ground truth: it is the only seed
corpus, and the whole test suite runs against it. Everything below was executed
against a database loaded from it.

The pipeline is two steps, deliberately separated so that the transliteration
can be re-run and diffed without touching the mapping:

```
seed/client/*.csv  --scripts/convert_client_csv.py-->  seed/*.json
seed/*.json        --scripts/seed_database.py------->  PostgreSQL
```

`scripts/convert_client_csv.py --check` asserts the JSON still matches the CSVs
and exits non-zero if not. That check exists because a partial conversion had
previously truncated two extracts at ten rows each, silently dropping the three
Critical entitlements that every SoD rule depends on.

### 1.1 The engine reproduces `peer_affinity_scores.csv` exactly

The client's affinity table is labelled "this is your Affinity Engine output".
Recomputing it from `identities.csv` with the existing service:

| Role group | Entitlements checked | Result |
|---|---|---|
| Financial Analyst / Finance | 4 | 4/4 match |
| Software Engineer / Technology | 3 | 3/3 match |
| Risk Analyst / Risk | 3 | 3/3 match |
| Internal Auditor / Audit | 3 | 3/3 match |

**13 of 13 values match on peer count, total peers and score.** No code changes.

The only representational difference is rounding: the client writes
`CONFLUENCE_USER` as `67`, the engine as `66.67` (2/3). Same value, different
convention — worth agreeing which is canonical for reporting.

### 1.2 The configured risk bands already agree with the client's

`entitlement_risk_scores.csv` carries a `risk_category` column (Low / Medium /
High / Critical) alongside the numeric score. Comparing it against the bands
already configured in this system (`0-30 / 31-69 / 70-89 / 90-100`):

**15 of 15 rows agree.** `RISK_LOW_MAX`, `RISK_MEDIUM_MAX` and `RISK_HIGH_MAX`
need no recalibration. Their `risk_category` is therefore redundant with the
derived band and should be treated as a cross-check, not a second source of
truth.

### 1.3 The worked example reproduces — with one discrepancy

Their POC walkthrough for `NJ1001` (Rahul Sharma) expects:

```
Recommended: SAP_FIN_DISPLAY, SAP_AP_INVOICE, POWERBI_FINANCE
```

The engine produces exactly that set, excluding `FIN_SHAREPOINT` at 20%
affinity. The generated SailPoint payload contains the same three entitlements.

**The discrepancy:** their walkthrough then states
`"status": "APPROVAL_REQUIRED", "approval_tier": "Manager"`. The engine returns
`AUTO_APPROVED` for all three — and by the client's *own* policy rules it should:
the three risk scores are 15, 45 and 10, while `POL005` triggers at ≥70 and
`POL006` at ≥90. Nothing in `policy_rules.csv` demands manager approval here.

Either there is an unstated blanket rule ("all provisioning requires manager
sign-off"), or the walkthrough was written by hand and is illustrative rather
than generated. **This needs confirming** — it is the difference between a fully
automated joiner path and one that always routes to a human.

---

## 2. Mapping applied

| Client file | Loaded into | Notes |
|---|---|---|
| `new_joiners.csv` | `employees` (`employment_status=PENDING_START`) | Column-for-column match |
| `identities.csv` | `employees` (`ACTIVE`) + `employee_entitlements` | `entitlements` column split on `;` |
| `entitlement_catalog.csv` + `entitlement_risk_scores.csv` | `entitlements` | Joined on entitlement **name** |
| `sod_rules.csv` | `sod_rules` | Near-exact; severity upper-cased |
| `policy_rules.csv` | `policies` | 2 of 7 loaded — see §4 |
| `peer_affinity_scores.csv` | *(not loaded)* | Used as a **validation fixture**, per §1.1 |

Loaded: 16 entitlements, 20 employees, 29 holdings, 3 SoD rules, 2 policies.

### The one schema change

`entitlements.external_id` (nullable). The catalogue's primary key is `ENT001`,
but **every other extract joins on the entitlement name** — `identities`,
`risk_scores`, `sod_rules` and `peer_affinity_scores` all use `SAP_FIN_DISPLAY`,
never `ENT001`. So the name is the real key; `ENT001` is retained for
source-system traceability only.

### Holdings — the blocking question from the RCA, now answered

The earlier RCA flagged that no entitlement-holdings table appeared in the
client's file list, and that without one the engine has no input. It resolves as
hypothesis 2: holdings are embedded in `identities.entitlements` as a
semicolon-delimited string. Splitting it yields 29 grant rows. **Not blocking.**

---

## 3. What the client's data cannot demonstrate

Running all ten joiners end to end:

```
outcomes   : AUTO_APPROVED 21 · NOT_RECOMMENDED 7 · HUMAN_REVIEW 3
strategies : exact match 7 · department fallback 2 · no peers 1
joiners with an SoD conflict: 0
```

### 3.1 No SoD conflict is reachable — at all

The three SoD rules reference `SAP_VENDOR_CREATE`, `SAP_PAYMENT_APPROVER` and
`AD_DOMAIN_ADMIN`. **No identity in `identities.csv` holds any of them.** They
therefore can never be recommended (nothing to derive affinity from) and are
never already-held, so no rule can ever fire.

The SoD engine is correct but inert against this dataset. To demonstrate SoD in
the POC the client needs either an identity holding one side of a pair, or a
peer group in which one appears. A single row — say a Finance identity holding
`SAP_VENDOR_CREATE` — would make `SOD003` fire against the recommended
`SAP_AP_INVOICE` (80% affinity) and produce a genuine block.

### 3.2 Their affinity table covers only 7 of 10 joiners

`peer_affinity_scores.csv` is keyed on `(job_role, department)`, and three
joiners have roles that appear nowhere in it:

| Joiner | Role | Covered by their table? | Engine behaviour |
|---|---|---|---|
| `NJ1008` Deepa Joseph | HR Specialist | No — no HR identities exist | 0 peers under every strategy; recommends nothing |
| `NJ1009` Vivek Kumar | Cloud Engineer | No | Falls back to `department` → 3 Technology peers, confidence 0.41 |
| `NJ1010` Arjun Patel | Senior Financial Analyst | No | Falls back to `department` → 5 Finance peers, confidence 0.47 |

This is the strongest argument for computing affinity rather than consuming
their table: **a precomputed lookup has no answer for a role it has never seen**,
which is precisely the new-joiner case that matters. The engine degrades
gracefully, records which strategy it fell back to, and lowers its confidence
accordingly.

`NJ1008` is also the honest case: no HR peers exist, so nothing is recommended
rather than something unrelated being invented.

---

## 4. The one genuine capability gap: birthright policies

Four of the seven policy rules are **grant** rules:

```
POL001,Finance Birthright,ALLOW,"Financial Analyst -> SAP_FIN_DISPLAY"
POL003,Engineering Birthright,ALLOW,"Software Engineer -> JIRA_USER"
```

Every policy evaluator currently implemented is a **restriction** — it can
block, deny or demand approval, but never grant. A birthright says the opposite:
this role should receive this entitlement *regardless of what peers happen to
hold*. The loader recognises these rules and deliberately refuses to load them
rather than mangle them into a restriction.

On this dataset it happens not to change any outcome — every birthright
entitlement also has 100% peer affinity, so it is recommended anyway. But the
semantics differ in the case that matters: a brand-new role with no peers (like
`NJ1008`) should still receive its birthright access, and today it receives
nothing.

### Proposed design

1. **New policy type** `ROLE_BIRTHRIGHT`, parameters `{job_role, entitlements[]}`.
2. **Candidate seeding** — birthright entitlements enter the candidate set even
   at 0% affinity, flagged `birthright=true` with the granting policy recorded.
3. **Affinity gate** — `decide()` currently sends anything below threshold to
   `NOT_RECOMMENDED` as its first test. Birthright bypasses that gate (and only
   that gate); risk, policy and SoD still apply in full. The decision trace
   records that the gate was bypassed and why.

This is contained: one enum member, one evaluator, one field on
`EntitlementAffinity`, one branch in `decide()`. It does **not** change the
schema. Estimated half a day including tests.

### Also noted

- `POL007 "affinity_score >= 70"` is modelled as a policy row, but it is the
  engine's recommendation threshold. It already exists as the
  `AFFINITY_THRESHOLD` setting (default 70.0 — the same number). Keeping it in
  one place avoids two sources of truth that can disagree. Worth confirming the
  client is content for it to live in configuration.
- `type` in `policy_rules.csv` (`ALLOW` / `HUMAN_APPROVAL`) corresponds to the
  *effect*, not the mechanism. The mapping is `HUMAN_APPROVAL → HUMAN_REVIEW`
  tier. `ALLOW` is only used on birthright and threshold rows.

---

## 5. Data quality findings

Raised by the loader; none are blocking, all are worth sending back.

| Finding | Detail | Handling |
|---|---|---|
| `SHAREPOINT_AUDIT` has no risk score | Held by `EMP010`, appears in the affinity table, absent from both `entitlement_catalog.csv` and `entitlement_risk_scores.csv` | Loaded as risk **100 / CRITICAL** so it fails closed to human review. An unscored entitlement is not automatically a safe one. |
| 5 entitlements missing from the catalogue | `POWERBI_RISK`, `POWERBI_AUDIT`, `SAP_PAYMENT_APPROVER`, `SAP_VENDOR_CREATE`, `AD_DOMAIN_ADMIN` are scored and/or referenced by SoD rules but absent from `entitlement_catalog.csv` | Loaded from the risk file; `owner` unknown |
| Catalogue is incomplete for SoD | All three SoD rules reference entitlements not in the catalogue | Works, but the catalogue is not the authoritative list it appears to be |
| `manager_id` unresolvable | `MGR100`–`MGR500` in `new_joiners.csv` are not identities in `identities.csv` | Stored as NULL. Manager-tier approval routing will need real manager identities. |
| `identities.csv` has no lifecycle column | No `employment_status`; leavers cannot be distinguished from active staff | All loaded as `ACTIVE`. **Worth flagging**: if the extract includes leavers, their access is currently shaping joiner recommendations. |
| Delimited multi-value column | `entitlements` is `;`-separated inside a CSV field | Parsed. Fragile if an entitlement name ever contains `;` |

The lifecycle column is the one with governance consequences. The system
deliberately excludes `TERMINATED` identities from peer groups; that protection
is inactive if the extract cannot express the status.

---

## 6. Recommendation

Keep the current structure. Specifically:

1. **Keep `scripts/seed_database.py` as the ingestion boundary.** The client's
   file shape is a source concern; the internal model stays as built.
2. **Use `peer_affinity_scores.json` as a regression fixture**, not an input —
   it already proves the engine correct, and it cannot answer for unseen roles.
   This is now enforced: `tests/integration/test_client_affinity.py` asserts all
   thirteen of their rows against a recomputation from `identities`.
3. **Build `ROLE_BIRTHRIGHT`** (§4). This is the only real gap.
4. **Send back the questions in §7** before hardening anything.

What was *not* required: any change to the peer, affinity, risk, SoD, decision,
explanation or SailPoint services; any change to the workflow; any change to the
MCP tools; any schema change beyond one nullable column.

---

## 7. Questions for the client

1. **The `NJ1001` walkthrough shows `APPROVAL_REQUIRED / Manager`, but the
   supplied policy rules produce auto-approval at those risk scores.** Is there
   an unstated rule that all joiner access requires manager approval? *(Changes
   whether the joiner path is automated at all.)*
2. **Does `identities.csv` contain only active staff?** There is no lifecycle
   column, so leavers cannot currently be excluded from peer groups.
3. **`SHAREPOINT_AUDIT` has no risk score.** Is it in scope? It is currently
   treated as CRITICAL so it fails closed.
4. **Should the entitlement catalogue be authoritative?** Five scored or
   SoD-referenced entitlements are missing from it.
5. **The SoD rules cannot fire against this dataset** (§3.1). Is that intended
   for the POC, or should the extract include an identity holding one side of a
   toxic pair so the control can be demonstrated?
6. **Birthright policies** (§4) — confirm the intent: should a role receive its
   birthright entitlements even when it has no peers (e.g. `NJ1008`, HR
   Specialist, for whom no peers exist)?
7. **`MGR100`–`MGR500`** are not present in `identities.csv`. Where do manager
   identities come from for approval routing?
8. **Rounding convention** for reported affinity — `67` or `66.67`?
