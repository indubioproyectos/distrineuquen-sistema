#!/usr/bin/env bash
# Step 10 of the 24/10/2026 cross: the transfers the re-audit of 25/10/2026
# found unloaded (repaso_cargar_body.py, input paso10_casos.csv): cards closed
# against Online drafts or a duplicate Online credit, and exact single-invoice
# matches. Needs steps 1 to 9 committed.
#   scripts/cruce-2026-10-24/paso10-repaso.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
private_copy "$CRUCE/paso10_casos.csv" paso10_casos.csv
private_copy "$CRUCE/online/creditos.json" creditos.json
private_copy "$CRUCE/arca/cuits_empleados.json" cuits_empleados.json
open_acl
run_body paso10 "$HERE/repaso_cargar_body.py"
