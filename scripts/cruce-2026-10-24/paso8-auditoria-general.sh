#!/usr/bin/env bash
# Step 8 of the 24/10/2026 cross: the corrections of the general audit of
# 25/10/2026 (auditoria/general/hallazgos.md, correcciones_general_body.py),
# read from paso8_casos.csv: cash payment 2528 back to its customer, the
# cards the module closed as «Ya en Online» by amount only reopened, the
# employees' hand-in transfers closed, and review notes on the lab payments
# that may be hand-ins. Needs steps 1 to 7 committed.
#   scripts/cruce-2026-10-24/paso8-auditoria-general.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
private_copy "$CRUCE/paso8_casos.csv" paso8_casos.csv
open_acl
run_body paso8 "$HERE/correcciones_general_body.py" ", PROBAR_SIN_PASO7='${PROBAR_SIN_PASO7:-0}'"
