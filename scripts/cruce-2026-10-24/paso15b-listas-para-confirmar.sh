#!/usr/bin/env bash
# Step 15b of the 24/10/2026 cross: marks the 16 group B cards that are safe
# to confirm (chatter note + shared board filter «Listas para confirmar
# (revisión 26/10)»). Confirms nothing (listas_para_confirmar_body.py).
#   scripts/cruce-2026-10-24/paso15b-listas-para-confirmar.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
open_acl
run_body paso15b "$HERE/listas_para_confirmar_body.py"
