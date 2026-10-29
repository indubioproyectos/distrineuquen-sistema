# Control 2026-10-28: receivable balance of every customer, with the same
# method the lab uses (res.partner._distri_account_balance: Odoo + imported
# history, deduplicated). READ ONLY: always rolls back. Writes one JSON to
# /out/balances-<TAG>.json: {partner_id: {name, ref, balance}}.
import json
import os

TAG = os.environ.get("SNAP_TAG", "snap")
P = env["res.partner"].sudo().with_context(active_test=False)
partners = P.search([("parent_id", "=", False), ("distri_generic_partner", "=", False)])
out = {}
for p in partners:
    bal = p._distri_account_balance()
    if abs(bal) < 0.005 and not p.ref:
        continue
    out[p.id] = {"name": p.name, "ref": p.ref or "", "balance": round(bal, 2),
                 "active": p.active}
path = "/out/balances-%s.json" % TAG
with open(path, "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False)
pos = sum(v["balance"] for v in out.values() if v["balance"] > 0)
print("BALANCES %s partners=%d positive_total=%.2f" % (TAG, len(out), pos))
env.cr.rollback()
