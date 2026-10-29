#!/usr/bin/env bash
# Step 14 of the 24/10/2026 cross: ties every receipt of the manager's chat,
# once imported (scripts/importar-chat-whatsapp.sh), to the transfer the cross
# and the vision review decided (unir_comprobantes_body.py, input
# paso14_comprobantes.csv). No payment or balance changes.
#   scripts/cruce-2026-10-24/paso14-unir-comprobantes.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
private_copy "$CRUCE/paso14_comprobantes.csv" paso14_comprobantes.csv
open_acl
run_body paso14 "$HERE/unir_comprobantes_body.py"
