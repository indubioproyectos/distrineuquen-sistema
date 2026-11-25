#!/usr/bin/env bash
# 19.0.1.129.24, one-off: closes the open driver charges («Cargos al
# repartidor») whose pending item is already settled (verified in Mercado
# Pago, or a «Pedido sin cobrar» paid / with its receipt), through
# distri.promised.transfer._distri_close_settled_charges. Idempotent. It only
# closes internal charges: it asserts the customers' receivable total and the
# number of entries, payments and reconciliations are unchanged.
# Needs 19.0.1.129.24 deployed (lab.sh loads the production snapshot for
# distri_lab).
# 19.0.1.129.32: the base is required (it used to default to production) and
# distri_lab with --apply needs --produccion.
#   scripts/correcciones/v129_24_cerrar_cargos.sh <base>            dry run: prints, writes nothing
#   scripts/correcciones/v129_24_cerrar_cargos.sh <base> --apply    closes and commits
set -euo pipefail
DB="" APPLY=0 PROD=0
for a in "$@"; do
  case $a in
    --apply) APPLY=1 ;;
    --produccion) PROD=1 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) DB=$a ;;
  esac
done
[[ -n "$DB" ]] || { echo "usage: $0 <base> [--apply] [--produccion]" >&2; exit 2; }
[[ "$DB" =~ ^[A-Za-z0-9_]+$ ]] || { echo "bad base name $DB" >&2; exit 2; }
if [[ "$DB" == distri_lab && $APPLY -eq 1 && $PROD -ne 1 ]]; then
  echo "distri_lab is production: pass --produccion only on the owner's order" >&2
  exit 1
fi
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
