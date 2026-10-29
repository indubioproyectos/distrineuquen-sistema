#!/usr/bin/env python3
"""Control 2026-10-28: the output files, from the copy's balances before and
after the control, the Online read of 28/10 and the control's logs.

  python3 -I reports.py

Writes in OUT (umask 077):
  decisiones.csv                 every change applied to the copy, with evidence
  comprobantes.csv               one row per WhatsApp receipt (the engine's evidence log)
  comparacion-online-vs-lab.csv  Online balance vs lab balance per customer, by cause
  cambios-vs-lab-vivo.csv        balance changes copy vs live lab, with the reason
  deudas-clientes.csv            the debt report data (management)
  deudas-clientes.md             the debt report (Spanish, management)
  resumen.json                   headline numbers
"""
import csv
import json
import os
from collections import defaultdict
from datetime import date

DATA = os.environ.get("CTL_DATA", "/tmp/claude-1000/-srv/297057ae-d6c4-4f4f-bef1-a067ba52a7c3/scratchpad/data")
WORK = "/srv/docs/distrineuquen-modulo/insumos/control-2026-10-28/work"
OUT = "/srv/docs/distrineuquen-modulo/insumos/control-2026-10-28"
ONLINE = "/srv/docs/distrineuquen-modulo/insumos/online/2026-10-28"
WHOLESALE_NAMES = {"JORGE USTIAS - MAYORISTA", "ANDRES MAYORISTA", "CRISTIAN CORREA - MAYORISTA"}
TODAY = "28/10/2026"
os.umask(0o077)


def read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(name, rows, fields=None):
    path = os.path.join(OUT, name)
    fields = fields or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.chmod(path, 0o600)
    return path


def money(x):
    s = "{:,.0f}".format(abs(x)).replace(",", ".")
    return ("-$ " if x < 0 else "$ ") + s


before = json.load(open(os.path.join(WORK, "balances-copia-inicial.json")))
after = json.load(open(os.path.join(WORK, "balances-copia-final.json")))
partners = {r["id"]: r for r in read(os.path.join(DATA, "partners_final.csv"))}
for pid, p in partners.items():
    p["wholesale"] = p["distri_es_mayorista"] == "t" or p["name"] in WHOLESALE_NAMES
    p["zapala"] = p["name"].startswith("Z -")
    p["frozen"] = p["zapala"] or pid == "315"

# ---------------------------------------------------------------- decisions
logs = []
for line in open(os.path.join(WORK, "apply-commit.log"), encoding="utf-8"):
    if line.startswith("LOG "):
        logs.append(json.loads(line[4:]))
decisions = []
for l in logs:
    a = l["action"]
    row = {"accion": a, "tarjeta": l.get("card", ""), "cliente": l.get("partner_name", l.get("new", "")),
           "importe": l.get("amount", ""), "pago_lab": l.get("payment", ""), "pago_online": l.get("online", ""),
           "comprobante_whatsapp": l.get("receipt", ""), "saldo_antes": l.get("balance_before", ""),
           "saldo_despues": l.get("balance_after", ""), "detalle": l.get("why", "")}
    row["efecto_en_saldo"] = {
        "cancel_lab_payment_link_online": "+%s (el pago del lab se anula: ya estaba en Online)" % money(l.get("amount", 0)),
        "confirm": "-%s (pago electrónico registrado en el lab)" % money(l.get("amount", 0)),
    }.get(a, "sin efecto")
    if a == "fix_card_partner":
        row["detalle"] = "cliente de la tarjeta %s → %s (el de Online)" % (l["old"], l["new"])
    decisions.append(row)
write_csv("decisiones.csv", decisions)

# ---------------------------------------------------------------- receipts
rows = read(os.path.join(WORK, "receipts.csv"))
write_csv("comprobantes.csv", rows)

# ---------------------------------------------------------------- Online vs lab (copy)
payload = json.load(open(os.path.join(ONLINE, "payload.json")))
exp = {int(k): v for k, v in payload["exp"].items()}
moves = defaultdict(lambda: defaultdict(float))
for r in read(os.path.join(DATA, "lab_moves_by_journal_final.csv")):
    moves[r["partner_id"]][r["code"]] += float(r["net"])
cmp_rows = []
for pid, p in partners.items():
    if not p["ref"].startswith("ODOO-"):
        continue
    oid = int(p["ref"][5:])
    online_bal = exp.get(oid, 0.0)
    lab_bal = after.get(pid, {}).get("balance", 0.0)
    m = moves.get(pid, {})
    diff = round(lab_bal - online_bal, 2)
    explained = round(sum(m.values()), 2)
    cmp_rows.append({
        "cliente_id": pid, "cliente": p["name"], "online_id": oid,
        "saldo_online": online_bal, "saldo_lab": lab_bal, "diferencia": diff,
        "pagos_mp_lab": round(m.get("MP", 0.0), 2), "efectivo_lab": round(m.get("CSH1", 0.0), 2),
        "ajustes_lab": round(m.get("AJC", 0.0), 2), "ventas_lab": round(m.get("VSR", 0.0), 2),
        "sin_explicar": round(diff - explained, 2),
        "nota": "Zapala: verificado con el gerente" if p["zapala"] else ("mayorista" if p["wholesale"] else ""),
    })
cmp_rows.sort(key=lambda r: -abs(r["diferencia"]))
write_csv("comparacion-online-vs-lab.csv", cmp_rows)
unexplained = [r for r in cmp_rows if abs(r["sin_explicar"]) > 0.5]

# ---------------------------------------------------------------- changes vs live lab
control_delta = defaultdict(float)
control_reason = defaultdict(list)
for l in logs:
    if l["action"] == "cancel_lab_payment_link_online":
        control_delta[str(l["partner"])] += l["amount"]
        control_reason[str(l["partner"])].append("se anula %s: ya estaba en Online como %s" % (l["payment"], l["online"]))
    elif l["action"] == "confirm":
        control_delta[str(l["partner"])] -= l["amount"]
        control_reason[str(l["partner"])].append("se registra %s (comprobante %s + reporte MP, no estaba en Online)" % (l["payment"], l["receipt"]))
# what the Online read of 28/10 brought (history rows after the lab's sync of 27/10 17:30)
clientes = {c["id"]: c["name"] for c in json.load(open(os.path.join(ONLINE, "clientes.json")))}
ref_to_pid = {int(p["ref"][5:]): pid for pid, p in partners.items() if p["ref"].startswith("ODOO-") and p["ref"][5:].isdigit()}
sync_reason = defaultdict(list)
for r in payload["new"]:
    pid = ref_to_pid.get(r["p"])
    if pid:
        sync_reason[pid].append("%s %s %s" % (r["c"], r["f"], money(r["d"] - r["h"])))
changes = []
for pid in set(before) | set(after):
    b = before.get(pid, {}).get("balance", 0.0)
    a = after.get(pid, {}).get("balance", 0.0)
    if abs(a - b) < 0.5:
        continue
    delta = round(a - b, 2)
    ctl = round(control_delta.get(pid, 0.0), 2)
    rest = round(delta - ctl, 2)
    reasons = list(control_reason.get(pid, []))
    if abs(rest) > 0.5:
        reasons.append("sincronización con Online del 28/10 01:30 (cargado en Online después del 27/10 17:30): " + "; ".join(sync_reason.get(pid, ["ver comparación"])))
    changes.append({"cliente_id": pid, "cliente": (after.get(pid) or before.get(pid))["name"], "saldo_lab_vivo": b,
                    "saldo_copia": a, "diferencia": delta, "por_el_control": ctl, "por_sincronizacion_online": rest,
                    "motivo": " | ".join(reasons)})
changes.sort(key=lambda r: r["diferencia"])
write_csv("cambios-vs-lab-vivo.csv", changes)

# ---------------------------------------------------------------- debt report
debtors, wholesale, zapala_rows = [], [], []
for pid, v in after.items():
    bal = v["balance"]
    if bal <= 0.5:
        continue
    p = partners.get(pid, {"name": v["name"], "wholesale": False, "zapala": False})
    row = {"cliente": v["name"], "deuda": round(bal, 2),
           "nota": "Verificado con el gerente (Zapala)" if p.get("zapala") else ""}
    if p.get("wholesale"):
        wholesale.append(row)
    else:
        debtors.append(row)
        if p.get("zapala"):
            zapala_rows.append(row)
debtors.sort(key=lambda r: -r["deuda"])
wholesale.sort(key=lambda r: -r["deuda"])
total = round(sum(r["deuda"] for r in debtors), 2)
total_w = round(sum(r["deuda"] for r in wholesale), 2)
credit = [v for v in after.values() if v["balance"] < -0.5 and not partners.get(next((k for k in after if after[k] is v), ""), {}).get("wholesale")]
credit_total = round(sum(v["balance"] for v in after.values() if v["balance"] < -0.5), 2)
write_csv("deudas-clientes.csv", [dict(r, grupo="minorista") for r in debtors] + [dict(r, grupo="mayorista (aparte)") for r in wholesale],
          ["grupo", "cliente", "deuda", "nota"])

no_receipt = read(os.path.join(OUT, "pagos-lab-sin-comprobante.csv"))
nr_retail = [r for r in no_receipt if not r["nota"]]
nr_total = round(sum(float(r["importe"]) for r in nr_retail), 2)

brackets = [(0, 100000), (100000, 300000), (300000, 500000), (500000, 1000000), (1000000, 10 ** 12)]
md = []
md.append("# Deudas de clientes minoristas\n")
md.append("Fecha: %s. Fuente: saldos de cuenta corriente del sistema de prueba (historial de Odoo Online al 28/10/2026 01:30 más los cobros registrados fuera de Online), después del control de pagos electrónicos.\n" % TODAY)
md.append("## Resumen\n")
md.append("| | |\n|---|---|")
md.append("| Clientes con deuda | %d |" % len(debtors))
md.append("| Deuda total | %s |" % money(total))
md.append("| Clientes de Zapala incluidos (saldos verificados con el gerente el 27/10) | %d, %s |" % (len(zapala_rows), money(sum(r["deuda"] for r in zapala_rows))))
md.append("| Mayoristas (fuera de este total, se informan aparte) | %d, %s |" % (len(wholesale), money(total_w)))
md.append("| Saldos a favor de clientes (no se restan; en general son ventas sin facturar) | %s |" % money(credit_total))
md.append("")
md.append("Se cuenta solo lo que los clientes deben: los saldos a favor no se restan del total.\n")
md.append("## Por tramo de deuda\n")
md.append("| Tramo | Clientes | Importe |\n|---|---|---|")
for lo, hi in brackets:
    grp = [r for r in debtors if lo < r["deuda"] <= hi]
    label = "hasta %s" % money(hi) if lo == 0 else ("más de %s" % money(lo) if hi > 10 ** 11 else "de %s a %s" % (money(lo), money(hi)))
    md.append("| %s | %d | %s |" % (label, len(grp), money(sum(r["deuda"] for r in grp))))
md.append("")
md.append("## Principales deudores\n")
md.append("| # | Cliente | Deuda | Observación |\n|---|---|---|---|")
for i, r in enumerate(debtors[:40], 1):
    md.append("| %d | %s | %s | %s |" % (i, r["cliente"], money(r["deuda"]), r["nota"]))
md.append("")
md.append("Los %d restantes suman %s. La lista completa está en `deudas-clientes.csv`.\n" % (
    max(0, len(debtors) - 40), money(sum(r["deuda"] for r in debtors[40:]))))
md.append("## Mayoristas (aparte)\n")
md.append("Se llevan en otro sistema; se listan solo como referencia y no entran en el total.\n")
md.append("| Cliente | Saldo |\n|---|---|")
for r in wholesale:
    md.append("| %s | %s |" % (r["cliente"], money(r["deuda"])))
md.append("")
md.append("## Qué se controló\n")
md.append("- Pagos electrónicos: se rehizo el cruce de cada comprobante del grupo de WhatsApp contra el reporte de Mercado Pago y contra Odoo Online. "
          "Resultado: se anularon %d cobros que estaban contados dos veces (ya cargados en Online) por %s y se registraron %d cobros con comprobante que faltaban por %s." % (
              sum(1 for l in logs if l["action"] == "cancel_lab_payment_link_online"),
              money(sum(l["amount"] for l in logs if l["action"] == "cancel_lab_payment_link_online")),
              sum(1 for l in logs if l["action"] == "confirm"),
              money(sum(l["amount"] for l in logs if l["action"] == "confirm"))))
md.append("- Pagos en efectivo de las planillas (22 al 27/10) y ajustes ya cargados: se mantienen como estaban.")
md.append("- Zapala: saldos verificados con el gerente el 27/10; no se recalcularon.")
md.append("- Quedan %d cobros electrónicos por %s registrados sin comprobante en el grupo (se identificaron por el CUIT del pagador o por el nombre en el resumen de Mercado Pago). "
          "Están contados a favor de los clientes. Si se decide aplicar la regla estricta (solo cuenta lo que tiene comprobante), la deuda total subiría en ese importe; la lista está en `pagos-lab-sin-comprobante.csv`." % (len(nr_retail), money(nr_total)))
md.append("")
path = os.path.join(OUT, "deudas-clientes.md")
open(path, "w", encoding="utf-8").write("\n".join(md))
os.chmod(path, 0o600)

summary = {
    "fecha": TODAY, "clientes_con_deuda": len(debtors), "deuda_total": total,
    "top10": debtors[:10], "mayoristas": wholesale, "total_mayoristas": total_w, "saldos_a_favor": credit_total,
    "zapala": {"clientes": len(zapala_rows), "importe": round(sum(r["deuda"] for r in zapala_rows), 2)},
    "sin_comprobante": {"pagos": len(nr_retail), "importe": nr_total},
    "cambios_vs_lab_vivo": len(changes),
    "control": {"anulados": [l for l in logs if l["action"] == "cancel_lab_payment_link_online"],
                "confirmados": [l for l in logs if l["action"] == "confirm"]},
    "online_vs_lab": {"clientes": len(cmp_rows), "sin_explicar": len(unexplained),
                      "sin_explicar_detalle": unexplained[:20]},
    "saldo_positivo_lab_vivo": round(sum(v["balance"] for v in before.values() if v["balance"] > 0), 2),
    "saldo_positivo_copia": round(sum(v["balance"] for v in after.values() if v["balance"] > 0), 2),
}
path = os.path.join(OUT, "resumen.json")
json.dump(summary, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
os.chmod(path, 0o600)
print(json.dumps({k: v for k, v in summary.items() if k not in ("control", "top10", "mayoristas", "online_vs_lab")}, ensure_ascii=False))
print("TOP10", [(r["cliente"], r["deuda"]) for r in debtors[:10]])
print("UNEXPLAINED", len(unexplained), [(r["cliente"], r["sin_explicar"]) for r in unexplained[:12]])
