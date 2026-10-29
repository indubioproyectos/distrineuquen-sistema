#!/usr/bin/env bash
# Step 1 of the 24/10/2026 cross: empties the collections board of the lab
# (scripts/online/limpiar_tablero_body.py, unchanged): Mercado Pago reports,
# incomes, outgoing transfers, daily closes and WhatsApp messages. Keeps the
# Online history and the customers. Stops if any card already has a payment.
#   scripts/cruce-2026-10-24/paso1-limpiar.sh [base]     default: distri_lab
#   DO_COMMIT=1 ...   writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
open_acl
run_body paso1 "$HERE/../online/limpiar_tablero_body.py"
