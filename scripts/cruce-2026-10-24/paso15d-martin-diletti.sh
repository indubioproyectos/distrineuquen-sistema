#!/usr/bin/env bash
# Step 15d of the 24/10/2026 cross: «Martin» (Luciano Martín Diletti, CUIT
# 20289479296) is only a reseller: the CUIT learned for ODOO-528 is removed,
# a payer rule «Re Venta Martin» is created and his 19 pending transfers are
# set aside (martin_diletti_body.py).
#   scripts/cruce-2026-10-24/paso15d-martin-diletti.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
open_acl
run_body paso15d "$HERE/martin_diletti_body.py"
