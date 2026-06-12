# Tower Lease Vetting Agent

A command-line agent that reads a tower lease request written in free-form natural language, checks it against tower capacity and regional policy, and returns a verdict: **APPROVED**, **REJECTED**, or **NEEDS REVIEW**.

Requests can be phrased however — different operators, unit conventions (kg or lb), number formats (digits or words), and tower id styles (`TWR-101`, `TWR101`, `tower 101`) are all handled without a fixed template.

---

## Quickstart

No dependencies beyond Python 3 for the offline mode.

```bash
# run the built-in test suite (7 cases, fully offline)
python tower_agent.py

# vet a single request
python tower_agent.py "Ooredoo wants a 55 lb antenna at twenty metres on tower 102"

# structured JSON output (useful for piping or integration)
python tower_agent.py --json "Du wants a 15kg antenna at 40 meters on TWR-101."

# run a batch queue and print the impact report
python tower_agent.py --batch
```

Keep `tower_agent.py`, `towers_inventory.json`, and `regional_policies.txt` in the same folder.

---

## Architecture

```
free-text input
     │
     ▼
 Extraction layer          ← AI (LLM tool loop) or offline regex parser
     │                        pulls: operator, asset, weight, height, tower id
     ▼
 Rules.decide()            ← deterministic code only — no AI involved here
     │                        checks: completeness → sanity → tower exists →
     │                                weight capacity → regional policy → confidence
     ▼
 APPROVED / REJECTED / NEEDS_REVIEW
     │
     ▼
 audit_log.jsonl           ← append-only record of every decision
```

**The AI never makes the verdict.** It handles the fuzzy part — reading varied natural language — while the rules handle everything that has to be exact and reproducible. A structural weight check is a safety decision; "the model said so" is not an acceptable audit trail entry. The clean separation also means the system works identically with no API key at all, falling back to the offline regex parser.

---

## Design decisions worth noting

**Unit normalisation.** Tenants write `33 lb` as often as `33 kg`. Reading the raw number and comparing it against a kg limit would silently mis-size the load — a 33 lb asset treated as 33 kg would wrongly clear a 25 kg regional cap. All weights are converted to kg before any check runs.

**Cumulative load tracking.** Two requests on the same tower can each fit in isolation but not together. As leases are approved, capacity is reserved in-session, so each subsequent request sees real remaining headroom rather than the original figure. This is the obvious failure mode the moment you process more than one request against the same tower.

**Confidence-gated routing.** If extraction confidence falls below the threshold — missing fields, ambiguous phrasing, multiple conflicting tower ids — the request is routed to a human reviewer even when the rules would have passed it. A held lease is better than a wrong auto-approval on a structure.

**Input robustness.** The offline parser handles: hyphenated units (`15-kg`, `40-meter`), spelled-out numbers (`forty metres`, `twenty-eight kilograms`), loose tower id formats (`TWR101`, `TWR 101`, `tower #102`), and unknown operators (name inferred from context). Failures degrade safely — a field that can't be read becomes missing, missing fields trigger `NEEDS_REVIEW`.

**Sanity checks before rules.** Zero or negative weight/height values are caught before reaching the rules engine and routed to review rather than matched against thresholds.

---

## Batch mode and impact reporting

`--batch` processes a queue of requests with a shared session (so cumulative load applies across the queue) and prints an impact summary:

```
IMPACT (this batch)
  requests processed     6
  auto-decided           5 (83%)
  routed to a human      1 (17%)
  recurring revenue      AED 312,960/yr from approved leases
  analyst time saved     2.1 hrs  (~AED 252)
```

The pricing and cost constants (`LEASE_BASE_FEE_AED`, `LEASE_PER_KG_AED`, `ANALYST_COST_PER_HOUR_AED`) are declared at the top of the file — drop in real numbers when available.

---

## Optional: AI extraction mode

The system runs fully offline by default. To enable the LLM extraction path:

```bash
pip install anthropic

# macOS / Linux
export ANTHROPIC_API_KEY=your-key

# Windows
set ANTHROPIC_API_KEY=your-key

python tower_agent.py --llm "Du wants a 15kg antenna at 40 meters on TWR-101."
```

The LLM path uses a tool-calling loop: the model is given `lookup_tower` and `get_regional_policies` tools to ground its reading against live inventory data, then calls `submit` with structured fields. If the API call fails for any reason, it falls back to the regex parser silently — the agent never crashes on a missing key or network error.

---

## Files

| File | Description |
|---|---|
| `tower_agent.py` | Agent, extraction logic, rules engine, CLI |
| `towers_inventory.json` | 120 towers across 6 regions (includes TWR-101 and TWR-102) |
| `regional_policies.txt` | Per-region height and weight limits |
| `gen_inventory.py` | Script used to generate the tower inventory |
| `audit_log.jsonl` | Created at runtime — append-only decision log |
