# Measures the double reading of receipt photos (19.0.1.55.0) against the
# amount Mercado Pago reported for the transfer each photo is tied to.
# Read only: nothing is written. SAMPLE photos at random plus every tied
# photo whose stored reading has no amount (up to MISSING).
import os
import random
import time

from odoo.addons.distri_pedido.models.ocr_comprobante import read_image

SAMPLE = int(os.environ.get("SAMPLE", "150"))
MISSING = int(os.environ.get("MISSING", "80"))
W = env["distri.whatsapp.message"].sudo()
base = [("distri_is_receipt", "=", True), ("message_type", "=", "image"),
        ("distri_collection_id.kind", "=", "income"),
        ("distri_collection_id.state", "=", "confirmed")]
tied = W.search(base)
missing = tied.filtered(lambda m: not m.distri_read_amount)
random.seed(29)
picked = random.sample(tied.ids, min(SAMPLE, len(tied))) + missing.ids[:MISSING]
print("TOTAL tied", len(tied), "without amount", len(missing), "measured", len(set(picked)))


def verdict(reading, truth):
    amount = reading.get("amount") or 0.0
    if not amount:
        return "none"
    ok = abs(amount - truth) < 0.5
    sure = reading.get("amount_confidence") == "high"
    return ("right" if ok else "WRONG") + ("_high" if sure else "_low")


counts = {}
for msg in W.browse(sorted(set(picked))):
    data = msg.attachment_id.raw
    truth = msg.distri_collection_id.amount
    t0 = time.time()
    _t, _c, old = read_image(data, double_check=False)
    t1 = time.time()
    _t, _c, new = read_image(data)
    t2 = time.time()
    key = (verdict(old, truth), verdict(new, truth))
    counts[key] = counts.get(key, 0) + 1
    print("ROW", msg.id, truth, old.get("amount"), old.get("amount_confidence"),
          new.get("amount"), new.get("amount_confidence"), key[0], key[1],
          round(t1 - t0, 1), round(t2 - t1, 1), flush=True)
for key, n in sorted(counts.items()):
    print("SUM", key[0], "->", key[1], n)
env.cr.rollback()
