#!/usr/bin/env python3
"""Compare two snapshots (snap.py): history rows and every customer balance.

The only balance difference allowed is the generic 15% tax of the replayed
lab-test invoices (Online never charged IVA). Exit code 1 on anything else.
"""
import json
import sys

a = json.load(open(sys.argv[1]))
b = json.load(open(sys.argv[2]))
ok = True
print("history", a["db"], a["history"])
print("history", b["db"], b["history"])
if a["history"] != b["history"]:
    ok = False
    print("HISTORY CHANGED")
expected = {}
for m in a["moves"]:
    if m["type"] == "out_invoice" and m["tax"]:
        expected[str(m["partner"])] = round(expected.get(str(m["partner"]), 0) - m["tax"], 2)
diffs = {}
for pid in sorted(set(a["balances"]) | set(b["balances"]), key=int):
    before, after = a["balances"].get(pid), b["balances"].get(pid)
    if before is None or after is None or abs(after - before) > 0.001:
        diffs[pid] = (before, after, None if None in (before, after) else round(after - before, 2))
same = len(a["balances"]) - len(diffs)
print("customers", len(a["balances"]), "->", len(b["balances"]), "identical", same, "different", len(diffs))
for pid, d in diffs.items():
    why = "lab-test invoice lost the generic 15% tax" if expected.get(pid) == d[2] else "UNEXPECTED"
    print("  partner", pid, d, why)
    if why == "UNEXPECTED":
        ok = False
print("total", a["total"], "->", b["total"])
for m in b["moves"]:
    print("  move", m["name"], m["type"], "partner", m["partner"], "total", m["total"], "tax", m["tax"])
print("OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
