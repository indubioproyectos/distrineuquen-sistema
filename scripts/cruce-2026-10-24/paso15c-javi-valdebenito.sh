#!/usr/bin/env bash
# Step 15c of the 24/10/2026 cross: CUIT 23346620609 on customer ODOO-565
# «JAVI BALD» (Javier César Valdebenito, MP «Distri Victoria») and his 16
# pending transfers set aside as reseller (javi_valdebenito_body.py).
#   scripts/cruce-2026-10-24/paso15c-javi-valdebenito.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
open_acl
run_body paso15c "$HERE/javi_valdebenito_body.py"
