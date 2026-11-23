# Loads reviewed allocations («Imputación revisada», distri.owed.allocation,
# 19.0.1.129.9) from propuesta.json files (odoo shell body, run by
# scripts/rojo/cargar-imputaciones.sh, which prepends IMPUTACIONES_B64: the
# files and the options, base64 JSON).
#
# Input: per file a list (or a single object) of
#   {"partner_id": N, "allocations": [{"credit_key", "debit_key", "amount",
#    "confidence": "segura|probable|dudosa", "why"}], ...}
# Keys are the statement rows' line_key (estado_cuenta.statement_line_key).
#
# Per customer: only the allocations whose confidence is in CONFIANZA; the
# previous active allocations of the same source (ORIGEN) are archived and
# the new ones created; the statement is recomputed and printed as red
# before -> after (number:amount), the check sum(owed) == max(balance, 0)
# and the allocations pass 0 skipped. A customer whose check fails by more
# than $1 is refused: his changes are undone and he is reported.
# Rolls back unless DO_COMMIT=1.
import base64
import json

from odoo.addons.distri_pedido.models.estado_cuenta import _money, mark_owed

DATA = json.loads(base64.b64decode(IMPUTACIONES_B64))  # noqa: F821 (prelude)
OPTS = DATA["env"]
DO_COMMIT = OPTS.get("DO_COMMIT") == "1"
ALLOWED = {c.strip() for c in (OPTS.get("CONFIANZA") or "segura,probable").split(",") if c.strip()}
SOURCE = (OPTS.get("ORIGEN") or "").strip() or "Revisión Fable"
APPROVED_BY = (OPTS.get("APROBADO_POR") or "").strip()
CHECK_TOLERANCE = 1.0

Alloc = env["distri.owed.allocation"].sudo()  # noqa: F821 (odoo shell)
Partner = env["res.partner"].sudo().with_context(active_test=False)  # noqa: F821


class Refused(Exception):
    pass


def out(text=""):
    print("IMP %s" % text if text else "IMP")


def red(rows):
    return ", ".join("%s:%s" % (r["number"] or r["line_key"], _money(r["owed"]))
                     for r in rows if r["owed"]) or "(nada)"


# 1. the proposals, merged per customer (a customer in two files adds up)
proposals, order, filtered = {}, [], {}
for name, content in DATA["files"]:
    items = content if isinstance(content, list) else [content]
    out("archivo %s: %d clientes" % (name, len(items)))
    for item in items:
        pid = int(item["partner_id"])
        if pid not in proposals:
            proposals[pid] = []
            order.append(pid)
        for a in item.get("allocations") or []:
            if a.get("confidence") not in ALLOWED:
                filtered[pid] = filtered.get(pid, 0) + 1
                continue
            proposals[pid].append(a)

out("origen «%s» · confianza %s · %s" % (
    SOURCE, ",".join(sorted(ALLOWED)), "ESCRIBE" if DO_COMMIT else "prueba (sin escribir)"))
totals = {"ok": 0, "refused": 0, "missing": 0, "created": 0, "archived": 0, "skipped": 0}
refused = []
for pid in order:
    partner = Partner.browse(pid).exists()
    if not partner:
        out("cliente %d: NO EXISTE, salteado" % pid)
        totals["missing"] += 1
        continue
    partner = partner.commercial_partner_id
    label = "%s [%d]" % (partner.name, partner.id)
    before = partner._distri_statement_rows()
    try:
        with env.cr.savepoint():  # noqa: F821
            old = Alloc.search([("partner_id", "=", partner.id), ("source", "=", SOURCE)])
            old.write({"active": False})
            new = Alloc.create([{
                "partner_id": partner.id,
                "credit_key": a["credit_key"],
                "debit_key": a["debit_key"],
                "amount": float(a["amount"]),
                "confidence": a["confidence"],
                "reason": a.get("why") or "",
                "source": SOURCE,
                "approved_by": APPROVED_BY,
            } for a in proposals[pid]])
            rows = partner._distri_statement_rows()
            stats = {}
            mark_owed(rows, stats, allocations=partner._distri_owed_allocations())
            owed = round(sum(r["owed"] for r in rows), 2)
            balance = round(rows[-1]["balance"], 2) if rows else 0.0
            skipped = stats.get("allocation_skipped") or []
            ok = abs(owed - max(balance, 0.0)) <= CHECK_TOLERANCE
            out("%s" % label)
            out("  imputaciones: %d nuevas · %d archivadas · %d descartadas por confianza"
                " · %d salteadas%s" % (
                    len(new), len(old), filtered.get(pid, 0), len(skipped),
                    (" (%s)" % ", ".join("%s→%s %s" % (a["credit_key"], a["debit_key"], why)
                                        for a, why in skipped)) if skipped else ""))
            out("  rojo antes:   %s" % red(before))
            out("  rojo después: %s" % red(rows))
            out("  control: rojo %s · saldo %s · %s" % (
                _money(owed), _money(balance), "OK" if ok else "NO CIERRA"))
            if not ok:
                raise Refused()
    except (Refused, Exception) as exc:  # bad data (amount <= 0, bad key...) refuses too
        env.invalidate_all()  # noqa: F821
        if not isinstance(exc, Refused):
            out("%s" % label)
            out("  ERROR: %s" % exc)
        out("  RECHAZADO: se deshacen las imputaciones de este cliente")
        totals["refused"] += 1
        refused.append(label)
        continue
    totals["ok"] += 1
    totals["created"] += len(new)
    totals["archived"] += len(old)
    totals["skipped"] += len(skipped)

out()
out("RESULT clientes ok %(ok)d · rechazados %(refused)d · inexistentes %(missing)d · "
    "imputaciones nuevas %(created)d · archivadas %(archived)d · salteadas %(skipped)d" % totals)
for label in refused:
    out("RECHAZADO %s" % label)
if DO_COMMIT:
    env.cr.commit()  # noqa: F821
    print("COMMITTED")
else:
    env.cr.rollback()  # noqa: F821
    print("NOT COMMITTED (DO_COMMIT=1 para escribir)")
