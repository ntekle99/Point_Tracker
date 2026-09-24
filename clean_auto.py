#!/usr/bin/env python3
"""
Remove auto-judged finds (auto:true) so the next autohunt.py re-prices them with
the current (tightened) judge. Hand-verified finds are kept. Run this, then
autohunt.py, to re-validate everything the old loose judge produced.
"""
import json
from pathlib import Path

F = Path(__file__).resolve().parent / "finds.json"
d = json.loads(F.read_text())
kept, removed = {}, []
for k, v in d.items():
    if k.startswith("_") or not (isinstance(v, dict) and v.get("auto")):
        kept[k] = v
    else:
        removed.append(k)
F.write_text(json.dumps(kept, indent=2))
print(f"removed {len(removed)} auto finds (kept {len(kept)-1} verified): {removed}")
print("next: python3 autohunt.py   # re-prices them with the tightened judge")
