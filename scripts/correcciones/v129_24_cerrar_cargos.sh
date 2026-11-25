#!/usr/bin/env bash
# 19.0.1.129.24, one-off: closes the open driver charges («Cargos al
# repartidor») whose pending item is already settled (verified in Mercado
# Pago, or a «Pedido sin cobrar» paid / with its receipt), through
# distri.promised.transfer._distri_close_settled_charges. Idempotent. It only
# closes internal charges: it asserts the customers' receivable total and the
# number of entries, payments and reconciliations are unchanged.
# Needs 19.0.1.129.24 deployed (lab.sh loads the production snapshot for
# distri_lab).
#   scripts/correcciones/v129_24_cerrar_cargos.sh [base]            dry run: prints, writes nothing
#   scripts/correcciones/v129_24_cerrar_cargos.sh [base] --apply    closes and commits
set -euo pipefail
DB=distri_lab APPLY=0
for a in "$@"; do
  case $a in --apply) APPLY=1 ;; *) DB=$a ;; esac
done
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=distri-cerrar-cargos-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http --log-level=warn <<PY 2>&1 \
  | rg -e '^(CLOSE|KEEP|RESULT|COMMITTED|DRY RUN)' -e 'Traceback' -e 'Error'
apply = $APPLY == 1
before = env["distri.promised.transfer"]._distri_ledger_fingerprint()
rows = env["distri.promised.transfer"]._distri_close_settled_charges(apply=apply)
for r in rows:
    print("CLOSE" if r["close"] else "KEEP ", r["case_id"], r["order"], r["driver"], r["amount"], "|", r["resolution"])
after = env["distri.promised.transfer"]._distri_ledger_fingerprint()
assert after == before, ("customer balances would change", before, after)
print("RESULT", {"close": sum(r["close"] for r in rows), "keep": sum(not r["close"] for r in rows),
                 "receivable_and_counts": after})
if apply:
    env.cr.commit()
    print("COMMITTED")
else:
    env.cr.rollback()
    print("DRY RUN: nothing written (pass --apply to close them)")
PY
