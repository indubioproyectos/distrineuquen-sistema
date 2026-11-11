#!/usr/bin/env python3
"""Read-only download of Odoo Online for the lab's catch-up (puesta al día).

Never writes to Online: every call goes through the read-only guard of
insumos/cruce/scripts/ro.py (search_read, read, search_count...). The key
lives in the prototype's .env and is never opened here.

  python3 scripts/online/leer_online.py [--desde AAAA-MM-DD]

Writes, in the private folder insumos/online/<today>/ (700, files 600):
  clientes.json        customers referenced by the receivable lines or payments
  lineas_cuenta.json   every posted receivable line (the history of each customer)
  pagos.json           customer payments (inbound), every state, with memo and journal
  ultima_factura.json  the last invoice number of the «0001» series
  payload.json         what scripts/online/aplicar_online_body.py applies to the lab:
                       new/changed/deleted history rows (rows corrected by hand in the lab,
                       marked by MANUAL_FIX, are left alone), new customers, expected
                       balance per customer and the payments mirror

The lab side (origen_id of each history row, customers by ref ODOO-<id>) is
read from the lab database with psql inside its container, read only.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

RO_DIR = "/srv/docs/distrineuquen-modulo/insumos/cruce/scripts"
OUT_ROOT = "/srv/docs/distrineuquen-modulo/insumos/online"
LAB_DB = os.environ.get("LAB_DB", "distri_lab")
BATCH = 2000
# A history row corrected by hand in the lab carries this mark in its concepto
# («… fecha corregida en el lab el 31/10/2026 …», «… pasado de ODOO-231 a
# ODOO-134 en el lab el 30/10/2026», «… archivado en el lab el …»): the sync
# never overwrites it and counts it for its lab customer in the balance check.
MANUAL_FIX = re.compile(r"en el lab el \d\d/\d\d/\d{4}")

LINE_FIELDS = ["id", "date", "move_id", "move_name", "move_type", "name", "ref", "journal_id",
               "partner_id", "debit", "credit", "amount_residual", "payment_id", "reconciled",
               "create_date"]
PAYMENT_FIELDS = ["id", "name", "date", "amount", "journal_id", "memo", "state", "partner_id",
                  "partner_type", "payment_type", "move_id", "create_date", "write_date",
                  "payment_reference"]
PARTNER_FIELDS = ["id", "name", "phone", "email", "street", "city", "is_company", "vat",
                  "parent_id", "commercial_partner_id", "active", "customer_rank"]


def ro():
    sys.path.insert(0, RO_DIR)
    import ro as module  # noqa: E402  (read-only guard)
    return module


def fetch_all(R, model, domain, fields, order="id"):
    total = R.call(model, "search_count", [domain])
    rows = []
    while len(rows) < total:
        rows += R.sr(model, domain, fields, offset=len(rows), limit=BATCH, order=order)
        print("  %s: %s/%s" % (model, len(rows), total), file=sys.stderr)
    return rows


def lab_sql(query):
    out = subprocess.run(["docker", "exec", "-i", "odoo-lab-db-1", "psql", "-U", "odoo", "-d",
                          LAB_DB, "-qAtX", "-F", "\t", "-c", query],
                         capture_output=True, text=True, check=True).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def m2o(value):
    return value[0] if isinstance(value, list) and value else None


def history_row(line):
    """Same rules as the 21/09 import (scripts/l10n_ar/sync_online_body.py)."""
    tipo = ("pago" if line["move_name"] else "otro") if line["payment_id"] else "factura"
    return {"o": line["id"], "p": m2o(line["partner_id"]), "f": line["date"],
            "c": line["move_name"] or "", "k": line["name"] or "", "t": tipo,
            "d": round(line["debit"] or 0.0, 2), "h": round(line["credit"] or 0.0, 2)}


def main():
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--desde", help="payments from this date only (AAAA-MM-DD)")
    opts = args.parse_args()
    R = ro()
    out = os.path.join(OUT_ROOT, dt.date.today().isoformat())
    os.makedirs(out, mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)

    print("Online: receivable lines", file=sys.stderr)
    lines = fetch_all(R, "account.move.line", [("account_id.account_type", "=", "asset_receivable"),
                                               ("parent_state", "=", "posted")], LINE_FIELDS)
    print("Online: payments", file=sys.stderr)
    pay_domain = [("payment_type", "=", "inbound"), ("partner_type", "=", "customer")]
    if opts.desde:
        pay_domain.append(("date", ">=", opts.desde))
    payments = fetch_all(R, "account.payment", pay_domain, PAYMENT_FIELDS)
    partner_ids = sorted({m2o(r["partner_id"]) for r in lines + payments if m2o(r["partner_id"])})
    print("Online: customers", file=sys.stderr)
    partners = []
    for i in range(0, len(partner_ids), BATCH):
        # search_read, not read: a contact of another company is skipped
        # instead of failing the whole batch
        partners += R.sr("res.partner", [("id", "in", partner_ids[i:i + BATCH]),
                                         ("active", "in", [True, False])], PARTNER_FIELDS)
    last = R.sr("account.move", [("move_type", "=", "out_invoice"), ("state", "=", "posted"),
                                 ("name", "=like", "0001/%")], ["name"], limit=1,
                order="name desc")
    last_inv = last[0]["name"] if last else ""

    # ---- the lab, read only
    lab_rows = {int(r[0]): r for r in lab_sql(
        "select m.origen_id, coalesce(to_char(m.fecha, 'YYYY-MM-DD'), ''), coalesce(m.comprobante, ''),"
        " coalesce(m.concepto, ''), round(coalesce(m.debe, 0), 2), round(coalesce(m.haber, 0), 2),"
        " coalesce(p.ref, '') from distri_cuenta_movimiento m left join res_partner p on p.id = m.partner_id"
        " where m.origen_id is not null and m.origen_id > 0")}
    lab_refs = {int(r[0][5:]) for r in lab_sql(
        "select ref from res_partner where ref like 'ODOO-%'") if r[0][5:].isdigit()}

    new, upd, protected = [], [], []
    online_ids = set()
    exp = {}
    for line in lines:
        row = history_row(line)
        online_ids.add(row["o"])
        lab = lab_rows.get(row["o"])
        if lab and MANUAL_FIX.search(lab[3]):
            # corrected by hand in the lab: the lab row stands (and counts for
            # its lab customer) until Online is fixed or the line disappears
            lab_pid = int(lab[6][5:]) if lab[6].startswith("ODOO-") and lab[6][5:].isdigit() else row["p"]
            if lab_pid:
                exp[lab_pid] = round(exp.get(lab_pid, 0.0) + float(lab[4]) - float(lab[5]), 2)
            if (lab[1], lab[2], float(lab[4]), float(lab[5]), lab_pid) != (
                    row["f"], row["c"], row["d"], row["h"], row["p"]):
                protected.append({"o": row["o"], "c": row["c"], "online": [row["f"], row["d"], row["h"], row["p"]],
                                  "lab": [lab[1], float(lab[4]), float(lab[5]), lab_pid]})
            continue
        if row["p"]:
            exp[row["p"]] = round(exp.get(row["p"], 0.0) + row["d"] - row["h"], 2)
        if not lab:
            new.append(row)
        elif (lab[1], lab[2], lab[3], float(lab[4]), float(lab[5])) != (
                row["f"], row["c"], row["k"], row["d"], row["h"]):
            upd.append(row)
    fixed_pay = {r["c"]: r["lab"] for r in protected if r["c"]}
    deleted = sorted(set(lab_rows) - online_ids)
    by_id = {p["id"]: p for p in partners}
    newp = []
    for pid in partner_ids:
        p = by_id.get(pid)
        if not p or pid in lab_refs:
            continue
        newp.append({"oid": pid, "name": p["name"], "phone": p["phone"] or "",
                     "email": p["email"] or "", "street": p["street"] or "",
                     "city": p["city"] or "", "is_company": p["is_company"],
                     "vat": p["vat"] or "", "parent": m2o(p["parent_id"])})
    mirror = [{"online_id": p["id"], "name": p["name"] or "", "date": p["date"],
               "amount": p["amount"], "journal_id": m2o(p["journal_id"]),
               "journal_name": (p["journal_id"] or [None, ""])[1],
               "memo": p["memo"] or p["payment_reference"] or "", "state": p["state"],
               "partner_id": m2o(p["partner_id"]),
               "move_name": (p["move_id"] or [None, ""])[1] if p["move_id"] else ""}
              for p in payments]
    for m in mirror:
        # the mirror of a payment corrected by hand keeps the lab's date, amount, customer
        fix = fixed_pay.get(m["move_name"]) or fixed_pay.get(m["name"])
        if fix:
            m.update(date=fix[0], amount=fix[1] or fix[2], partner_id=fix[3])
    payload = {"read_at": dt.datetime.now().isoformat(timespec="seconds"),
               "newp": newp, "updp": [], "new": new, "upd": upd, "del": deleted,
               "last_inv": last_inv, "exp": {str(k): v for k, v in exp.items()},
               "payments": mirror, "protected": protected}

    def dump(name, data):
        path = os.path.join(out, name)
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                  encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, default=str)
        os.chmod(path, 0o600)

    dump("lineas_cuenta.json", lines)
    dump("pagos.json", payments)
    dump("clientes.json", partners)
    dump("ultima_factura.json", {"name": last_inv})
    dump("payload.json", payload)
    print(json.dumps({"carpeta": out, "lineas": len(lines), "pagos": len(payments),
                      "clientes": len(partners), "filas_nuevas": len(new),
                      "filas_cambiadas": len(upd), "filas_borradas": len(deleted),
                      "clientes_nuevos": len(newp), "ultima_factura": bool(last_inv),
                      "correcciones_del_lab_respetadas": len(protected)}))


if __name__ == "__main__":
    main()
