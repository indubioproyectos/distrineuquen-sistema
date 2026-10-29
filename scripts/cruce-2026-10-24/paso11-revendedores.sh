#!/usr/bin/env bash
# Step 11 of the 24/10/2026 cross: the payers the manager classified as
# resellers/distributors (or their customers) leave the retail matching:
# payer rules by CUIT and their pending transfers set aside
# (apartar_revendedores_body.py, input paso11_revendedores.csv). No balance
# changes. Needs steps 1 to 10 committed.
#   scripts/cruce-2026-10-24/paso11-revendedores.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
private_copy "$CRUCE/paso11_revendedores.csv" paso11_revendedores.csv
open_acl
run_body paso11 "$HERE/apartar_revendedores_body.py"
