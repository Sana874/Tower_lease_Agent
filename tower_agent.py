# pyright: basic

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
INVENTORY_FILE = HERE / "towers_inventory.json"
POLICY_FILE = HERE / "regional_policies.txt"
AUDIT_FILE = HERE / "audit_log.jsonl"

LB_TO_KG = 0.453592

LEASE_BASE_FEE_AED = 4000        
LEASE_PER_KG_AED = 80            
MANUAL_REVIEW_MINUTES = 25       
ANALYST_COST_PER_HOUR_AED = 120

CONFIDENCE_FLOOR = 0.6


# --- domain types ------------------------------------------------------------

@dataclass
class Request:
    raw_text: str
    operator: str | None = None
    asset: str | None = None
    weight_kg: float | None = None
    height_m: float | None = None
    tower_id: str | None = None
    confidence: float = 1.0
    notes: list[str] = field(default_factory=list)


@dataclass
class Policy:
    region: str
    max_height_m: float | None = None
    max_asset_kg: float | None = None


@dataclass
class Decision:
    verdict: str                       # APPROVED / REJECTED / NEEDS_REVIEW
    operator: str | None
    tower_id: str | None
    checks: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    confidence: float = 1.0
    extraction: str = "regex"
    monthly_fee_aed: float | None = None
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append({"check": name, "ok": ok, "detail": detail})
        return ok

    def json(self) -> str:
        return json.dumps(asdict(self), indent=2)


# --- data access (these are the tools the agent calls) -----------------------

class Inventory:
    def __init__(self, path: Path = INVENTORY_FILE, policy_path: Path = POLICY_FILE):
        self.towers = {t["tower_id"]: dict(t) for t in json.loads(path.read_text())}
        self.policies = self._load_policies(policy_path.read_text())
        # tracks weight committed this session so back-to-back approvals on the
        # same tower don't each see the original headroom (the real race).
        self.reserved: dict[str, float] = {}

    @staticmethod
    def _load_policies(text: str) -> dict[str, Policy]:
        out: dict[str, Policy] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            zone, rule = line.split(":", 1)
            region = zone.replace("Zone", "").strip()
            p = out.setdefault(region, Policy(region))
            h = re.search(r"height\D*(\d+(?:\.\d+)?)\s*(?:m|meter)", rule, re.I)
            if h:
                p.max_height_m = float(h.group(1))
            w = re.search(r"exceed\D*(\d+(?:\.\d+)?)\s*kg", rule, re.I)
            if w:
                p.max_asset_kg = float(w.group(1))
        return out

    def get_tower(self, tower_id: str) -> dict | None:
        return self.towers.get(tower_id.strip().upper())

    def get_policy(self, region: str) -> Policy:
        return self.policies.get(region, Policy(region))

    def headroom(self, tower_id: str) -> float:
        t = self.towers[tower_id]
        return t["max_allowed_weight_kg"] - t["current_weight_kg"] - self.reserved.get(tower_id, 0.0)

    def reserve(self, tower_id: str, kg: float) -> None:
        self.reserved[tower_id] = self.reserved.get(tower_id, 0.0) + kg


# --- understanding the request -----------------------------------------------

OPERATORS = ["Etisalat", "e&", "Du", "Vodafone", "Verizon", "Zain", "STC",
             "Ooredoo", "Batelco", "Orange"]


def parse_weight(text: str) -> tuple[float | None, list[str]]:
    """Pull a weight and normalise to kg. Pounds are a real trap here - a 33 lb
    asset read as 33 kg can wrongly clear a load check, so convert explicitly."""
    notes: list[str] = []
    lb = re.search(r"(\d+(?:\.\d+)?)\s*(?:lb|lbs|pound)", text, re.I)
    if lb:
        kg = round(float(lb.group(1)) * LB_TO_KG, 2)
        notes.append(f"converted {lb.group(1)} lb -> {kg} kg")
        return kg, notes
    kg = re.search(r"(\d+(?:\.\d+)?)\s*(?:kg|kilo|kilogram)", text, re.I)
    if kg:
        return float(kg.group(1)), notes
    return None, notes


def extract_regex(text: str) -> Request:
    req = Request(raw_text=text)

    tower = re.findall(r"\bTWR-\d+\b", text, re.I)
    if tower:
        req.tower_id = tower[0].upper()
        if len(set(t.upper() for t in tower)) > 1:
            req.notes.append("multiple tower IDs found; used the first")

    req.weight_kg, wnotes = parse_weight(text)
    req.notes += wnotes

    h = re.search(r"(\d+(?:\.\d+)?)\s*(?:m\b|meter|metre)", text, re.I)
    if h:
        req.height_m = float(h.group(1))

    for op in OPERATORS:
        if re.search(rf"\b{re.escape(op)}\b", text, re.I):
            req.operator = op
            break

    a = re.search(r"((?:\w+\s+){0,2}(?:antenna|radio|dish|unit|repeater|panel))", text, re.I)
    if a:
        req.asset = a.group(1).strip()

    found = sum(x is not None for x in (req.weight_kg, req.height_m, req.tower_id))
    req.confidence = round(found / 3 - 0.15 * len(req.notes), 2)
    req.confidence = max(0.0, min(1.0, req.confidence))
    return req


def extract_llm(text: str, inv: Inventory) -> Request:
    """Agentic extraction: the model is given the lookup tools and decides which
    to call to ground its reading of the request. We still let our own rules make
    the final call - the model only gathers and structures. Falls back to regex
    if anthropic isn't installed or no key is set."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return extract_regex(text)
    try:
        import anthropic  # type: ignore
    except ImportError:
        return extract_regex(text)

    tools: list[dict[str, Any]] = [
        {"name": "lookup_tower",
         "description": "Get a tower's region and weight capacity by id.",
         "input_schema": {"type": "object",
                          "properties": {"tower_id": {"type": "string"}},
                          "required": ["tower_id"]}},
        {"name": "get_regional_policies",
         "description": "Get height/weight limits for a region.",
         "input_schema": {"type": "object",
                          "properties": {"region": {"type": "string"}},
                          "required": ["region"]}},
        {"name": "submit",
         "description": "Submit the parsed request once you have the facts.",
         "input_schema": {"type": "object", "properties": {
             "operator": {"type": "string"}, "asset": {"type": "string"},
             "weight_kg": {"type": "number"}, "height_m": {"type": "number"},
             "tower_id": {"type": "string"},
             "confidence": {"type": "number",
                            "description": "0-1, how sure you are of the extraction"}},
             "required": ["weight_kg", "height_m", "tower_id", "confidence"]}},
    ]

    client: Any = anthropic.Anthropic()
    messages: list[dict[str, Any]] = [{"role": "user", "content":
                 "Parse this tower lease request. Use the tools to confirm the "
                 "tower and its region exist, convert any pounds to kg, then call "
                 f"submit.\n\n{text}"}]

    try:
        for _ in range(6):  # bound the loop
            resp: Any = client.messages.create(model="claude-sonnet-4-6", max_tokens=1024,
                                               tools=tools, messages=messages)
            messages.append({"role": "assistant", "content": resp.content})
            calls: list[Any] = [
                b for b in resp.content if getattr(b, "type", "") == "tool_use"
            ]
            if not calls:
                break
            results = []
            for c in calls:
                if c.name == "submit":
                    d = c.input
                    return Request(raw_text=text, operator=d.get("operator"),
                                   asset=d.get("asset"), weight_kg=d.get("weight_kg"),
                                   height_m=d.get("height_m"),
                                   tower_id=str(d.get("tower_id", "")).upper() or None,
                                   confidence=float(d.get("confidence", 0.8)),
                                   notes=["extracted via LLM tool loop"])
                elif c.name == "lookup_tower":
                    results.append((c.id, inv.get_tower(c.input.get("tower_id", "")) or "not found"))
                elif c.name == "get_regional_policies":
                    results.append((c.id, asdict(inv.get_policy(c.input.get("region", "")))))
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": json.dumps(r)}
                for tid, r in results]})
    except Exception as e:
        # network / auth / model / parsing - don't crash, just use the offline path
        req = extract_regex(text)
        req.notes.append(f"LLM call failed ({type(e).__name__}); used regex instead")
        return req

    return extract_regex(text)  # model never submitted; fall back

# --- the rules (single source of truth for the verdict) ----------------------

class Rules:
    def __init__(self, inv: Inventory):
        self.inv = inv

    def decide(self, req: Request, extraction: str = "regex") -> Decision:
        d = Decision(verdict="APPROVED", operator=req.operator, tower_id=req.tower_id,
                     confidence=req.confidence, extraction=extraction,
                     reasons=list(req.notes))

        missing = [n for n, v in (("weight_kg", req.weight_kg),
                                  ("height_m", req.height_m),
                                  ("tower_id", req.tower_id)) if v is None]
        if missing:
            d.check("complete", False, "missing: " + ", ".join(missing))
            d.verdict = "NEEDS_REVIEW"
            d.reasons.append("Couldn't read: " + ", ".join(missing) + ". Sending to a human.")
            return d
        d.check("complete", True, "all fields present")

        assert req.weight_kg is not None and req.height_m is not None and req.tower_id is not None

        # sanity check the numbers before trusting them
        if req.weight_kg <= 0 or req.height_m <= 0:
            d.check("plausible_values", False,
                    f"weight={req.weight_kg}, height={req.height_m}")
            d.verdict = "NEEDS_REVIEW"
            d.reasons.append("Non-physical weight or height; needs a human to check the request.")
            return d

        tower = self.inv.get_tower(req.tower_id)
        if tower is None:
            d.check("tower_exists", False, f"{req.tower_id} not in inventory")
            d.verdict = "REJECTED"
            d.reasons.append(f"No tower {req.tower_id}.")
            return d
        region = tower["region"]
        d.check("tower_exists", True, f"{req.tower_id} in {region}")
        policy = self.inv.get_policy(region)

        room = self.inv.headroom(req.tower_id)
        if not d.check("weight_capacity", req.weight_kg <= room,
                       f"need {req.weight_kg}kg, {room}kg free on {req.tower_id}"):
            d.verdict = "REJECTED"
            d.reasons.append(f"Overloads {req.tower_id}: only {room}kg free, asked {req.weight_kg}kg.")

        if policy.max_asset_kg is not None:
            if not d.check("regional_weight", req.weight_kg <= policy.max_asset_kg,
                           f"{req.weight_kg}kg vs {policy.max_asset_kg}kg cap in {region}"):
                d.verdict = "REJECTED"
                d.reasons.append(f"{region} caps single assets at {policy.max_asset_kg}kg.")

        if policy.max_height_m is not None:
            if not d.check("regional_height", req.height_m <= policy.max_height_m,
                           f"{req.height_m}m vs {policy.max_height_m}m cap in {region}"):
                d.verdict = "REJECTED"
                d.reasons.append(f"{region} caps height at {policy.max_height_m}m.")

        # would-pass but we're not confident we read it right
        if d.verdict == "APPROVED" and req.confidence < CONFIDENCE_FLOOR:
            d.verdict = "NEEDS_REVIEW"
            d.reasons.append(f"Rules pass but extraction confidence is low ({req.confidence}); "
                             "routing to a human.")
            return d

        if d.verdict == "APPROVED":
            d.monthly_fee_aed = LEASE_BASE_FEE_AED + LEASE_PER_KG_AED * req.weight_kg
            self.inv.reserve(req.tower_id, req.weight_kg)
            d.reasons.append("All checks pass.")
        return d


# --- agent (wires it together + logs) ----------------------------------------

class Agent:
    def __init__(self, inv: Inventory | None = None):
        self.inv = inv or Inventory()
        self.rules = Rules(self.inv)

    def handle(self, text: str, use_llm: bool = False) -> Decision:
        if use_llm:
            req = extract_llm(text, self.inv)
            # only call it 'llm' if the model actually did the extraction
            mode = "llm" if "extracted via LLM tool loop" in req.notes else "regex"
        else:
            req = extract_regex(text)
            mode = "regex"
        decision = self.rules.decide(req, extraction=mode)
        self._audit(text, decision)
        return decision

    def _audit(self, text: str, d: Decision) -> None:
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps({"at": d.at, "request": text, "verdict": d.verdict,
                                "tower": d.tower_id, "confidence": d.confidence}) + "\n")


# --- impact report -----------------------------------------------------------

def impact_report(decisions: list[Decision]) -> str:
    n = len(decisions)
    if n == 0:
        return "IMPACT (this batch)\n  no requests processed"
    auto = [d for d in decisions if d.verdict in ("APPROVED", "REJECTED")]
    review = [d for d in decisions if d.verdict == "NEEDS_REVIEW"]
    arr = sum((d.monthly_fee_aed or 0) for d in decisions) * 12
    hours_saved = len(auto) * MANUAL_REVIEW_MINUTES / 60
    cost_saved = hours_saved * ANALYST_COST_PER_HOUR_AED

    lines = [
        "IMPACT (this batch)",
        f"  requests processed     {n}",
        f"  auto-decided           {len(auto)} ({len(auto) / n:.0%})",
        f"  routed to a human      {len(review)} ({len(review) / n:.0%})",
        f"  recurring revenue      AED {arr:,.0f}/yr from approved leases",
        f"  analyst time saved     {hours_saved:.1f} hrs  (~AED {cost_saved:,.0f})",
        "",
        "  Assumptions: AED %d base + AED %d/kg per asset/month; %d min manual"
        % (LEASE_BASE_FEE_AED, LEASE_PER_KG_AED, MANUAL_REVIEW_MINUTES),
        "  review at AED %d/hr." % ANALYST_COST_PER_HOUR_AED,
    ]
    return "\n".join(lines)


# --- cli / tests -------------------------------------------------------------

def show(d: Decision) -> None:
    print(f"\n{d.verdict}  (conf {d.confidence}, {d.extraction})")
    for c in d.checks:
        print(f"   [{'ok' if c['ok'] else 'x'}] {c['check']}: {c['detail']}")
    for r in d.reasons:
        print(f"   - {r}")
    if d.monthly_fee_aed:
        print(f"   fee: AED {d.monthly_fee_aed:,.0f}/mo")


def run_tests() -> None:
    agent = Agent()
    cases = [
        ("Operator Du wants to mount a 15kg 5G antenna at a height of 40 meters on Tower TWR-101.", "APPROVED"),
        ("Vodafone wants a 60kg radio unit at 30 meters on TWR-101.", "REJECTED"),
        ("Etisalat requests a 28kg antenna at 20 meters on TWR-102.", "REJECTED"),
        ("Du wants a 10kg dish at a height of 50 meters on TWR-101.", "REJECTED"),
        ("Verizon wants a 5kg unit at 10 meters on TWR-999.", "REJECTED"),
        ("Du wants a 12kg antenna on TWR-101.", "NEEDS_REVIEW"),
        ("Ooredoo wants a 55 lb antenna at 20 meters on TWR-102.", "APPROVED"),
    ]
    passed = 0
    for text, want in cases:
        d = agent.handle(text)
        ok = d.verdict == want
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] expected {want}: {text[:60]}...")
        show(d)
    print(f"\n{passed}/{len(cases)} passed")


def run_batch() -> None:
    agent = Agent()
    queue = [
        "Operator Du wants to mount a 15kg 5G antenna at 40 meters on TWR-101.",
        "Etisalat wants a 25kg radio at 30 meters on TWR-101.",
        "Virgin Mobile wants a 25kg antenna at 30 meters on TWR-101.",
        "Du wants a 33 lb dish at 35 meters on TWR-103.",   # pounds -> kg
        "Ooredoo wants a 30kg antenna at 22 meters on TWR-102.",  
        "Verizon wants a unit on TWR-101.",                 
    ]
    results = []
    for text in queue:
        print(f"\n> {text}")
        d = agent.handle(text)
        show(d)
        results.append(d)
    print("\n" + "=" * 60)
    print(impact_report(results))


def main(argv: list[str]) -> None:
    use_llm = "--llm" in argv
    as_json = "--json" in argv
    argv = [a for a in argv if a not in ("--llm", "--json")]
    if "--batch" in argv:
        run_batch()
    elif len(argv) > 1:
        d = Agent().handle(" ".join(argv[1:]), use_llm=use_llm)
        if as_json:
            print(d.json())   # structured response
        else:
            show(d)
    else:
        run_tests()


if __name__ == "__main__":
    main(sys.argv)