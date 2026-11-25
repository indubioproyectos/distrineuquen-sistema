#!/usr/bin/env bash
# 19.0.1.129.27, one-off (owner's decision A of 24/11/2026): books the cash
# shortfall cases still open (distri.route.case kind «cash», negative, not
# replenished, not booked yet: the cashier's own included, e.g. route 129 of
# Italo for $ 600) as the EMPLOYEE's debt in «Deudores varios»
# (1.1.5.01.010) under his own res.users partner, never a customer:
#     Deudores varios (employee)   debit
#     counterpart                  credit  (default «Diferencias de caja»,
#                                           where the closing booked it)
# through distri.route.case._distri_book_opening_debts (models/caja_v2.py).
# Idempotent: a booked case keeps its entry (debt_move_id) and is skipped.
# It asserts the customers' receivable total, the employees' partners left
# out, is unchanged. The owner confirms the counterpart and the date first.
# Needs 19.0.1.129.27 deployed on that database.
#   scripts/correcciones/v129_27_deudas_iniciales.sh [base] [opciones]          dry run: prints, writes nothing
#   scripts/correcciones/v129_27_deudas_iniciales.sh [base] [opciones] --apply  books and commits
# Options:
#   --date=AAAA-MM-DD        date of the entries (default: today)
#   --counterpart=CODE       counterpart account code (default: «Diferencias de caja»)
#   --journal=CODE           general journal code (default: the first general journal)
#   --cases=21,22,...        only those cases (default: every open shortfall)
# The base distri_practica runs in the practice environment
# (practica/comun.sh odoo_run); any other one through scripts/l10n_ar/lab.sh.
set -euo pipefail
DB=distri_lab APPLY=0 DATE="" COUNTERPART="" JOURNAL="" CASES=""
for a in "$@"; do
  case $a in
    --apply) APPLY=1 ;;
    --date=*) DATE=${a#--date=} ;;
    --counterpart=*) COUNTERPART=${a#--counterpart=} ;;
    --journal=*) JOURNAL=${a#--journal=} ;;
    --cases=*) CASES=${a#--cases=} ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) DB=$a ;;
  esac
done
[[ -z "$DATE" || "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "bad --date $DATE" >&2; exit 2; }
[[ "$COUNTERPART$JOURNAL" =~ ^[A-Za-z0-9.]*$ ]] || { echo "bad account or journal code" >&2; exit 2; }
[[ "$CASES" =~ ^[0-9,]*$ ]] || { echo "bad --cases $CASES" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)

PY=$(cat <<PY
apply = $APPLY == 1
cases = [int(x) for x in "$CASES".split(",") if x] or None
env = env(user=2)
res = env["distri.route.case"]._distri_book_opening_debts(
    apply=apply, date="$DATE" or None, counterpart_code="$COUNTERPART" or None,
    journal_code="$JOURNAL" or None, case_ids=cases)
for r in res["rows"]:
    if r["error"]:
        print("SKIP ", r["case_id"], r["case"], "|", r["error"])
    else:
        print("DEBT ", r["case_id"], r["case"], "|", r["employee"], "-> partner", r["partner_id"],
              r["partner"], "|", r["amount"], "|", r["move"] or "(dry run)")
print("RESULT", {"debts": sum(1 for r in res["rows"] if not r["error"]),
                 "total": sum(r["amount"] for r in res["rows"] if not r["error"]),
                 "date": res["date"], "journal": res["journal"], "debit": res["debt_account"],
                 "credit": res["counterpart"], "customers_receivable": res["customers_receivable"]})
if apply:
    env.cr.commit()
    print("COMMITTED")
else:
    env.cr.rollback()
    print("DRY RUN: nothing written (pass --apply to book them)")
PY
)
FILTER=(rg -e '^(DEBT|SKIP|RESULT|COMMITTED|DRY RUN)' -e 'Traceback' -e 'Error')
if [[ "$DB" == distri_practica ]]; then
  # shellcheck source=../../practica/comun.sh
  source "$HERE/../../practica/comun.sh"
  odoo_run shell -d "$DB" --no-http --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
else
  NAME=distri-deudas-iniciales-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http \
    --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
fi
