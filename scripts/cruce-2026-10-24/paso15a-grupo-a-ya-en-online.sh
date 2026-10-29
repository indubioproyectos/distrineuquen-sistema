#!/usr/bin/env bash
# Step 15a of the 24/10/2026 cross: closes the 44 Mercado Pago income cards of
# group A (revision of 26/10/2026) that are already registered in Online, as
# «Ya cargado en Online» (paso15a_body.py; the list is embedded there, no
# input file). No payment or balance changes.
#   scripts/cruce-2026-10-24/paso15a-grupo-a-ya-en-online.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
open_acl
run_body paso15a "$HERE/paso15a_body.py"
