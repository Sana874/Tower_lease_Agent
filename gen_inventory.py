import json, random
from pathlib import Path
random.seed(42)

regions = ["DXB-North", "DXB-South", "SHJ-Coastal", "SHJ-South", "AUH-Central", "AUH-Coastal"]
towers = [
    {"tower_id": "TWR-101", "region": "DXB-North",   "max_allowed_weight_kg": 500, "current_weight_kg": 460},
    {"tower_id": "TWR-102", "region": "SHJ-Coastal", "max_allowed_weight_kg": 300, "current_weight_kg": 120},
]
for i in range(103, 221):
    region = random.choice(regions)
    cap = random.choice([300, 350, 400, 450, 500, 600, 750])
    cur = random.randint(int(cap*0.2), int(cap*0.95))
    towers.append({
        "tower_id": f"TWR-{i}",
        "region": region,
        "max_allowed_weight_kg": cap,
        "current_weight_kg": cur,
    })
OUT = Path(__file__).resolve().parent / "towers_inventory.json"
json.dump(towers, OUT.open("w"), indent=2)
print(f"wrote {len(towers)} towers")