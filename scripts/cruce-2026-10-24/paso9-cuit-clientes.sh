#!/usr/bin/env bash
# Step 9 of the 24/10/2026 cross: the CUIT of the 19 customers whose name
# carries their DNI, found in the ARCA padrón (cuit_clientes_body.py, input
# arca/cuit_por_dni.csv). Nothing is merged. Needs steps 1 to 8 committed.
#   scripts/cruce-2026-10-24/paso9-cuit-clientes.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
private_copy "$CRUCE/arca/cuit_por_dni.csv" cuit_por_dni.csv
open_acl
run_body paso9 "$HERE/cuit_clientes_body.py"
