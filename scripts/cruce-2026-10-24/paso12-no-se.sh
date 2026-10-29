#!/usr/bin/env bash
# Step 12 of the 24/10/2026 cross: from the review of the payers the manager
# left as «No sé» (no_se_body.py): wrong payer identities learned on 29/09
# removed, and the transfers whose chat receipts name a retail customer
# loaded. Needs steps 1 to 11 committed.
#   scripts/cruce-2026-10-24/paso12-no-se.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
private_copy "$CRUCE/paso12_casos.csv" paso12_casos.csv
private_copy "$CRUCE/paso12_identidades.csv" paso12_identidades.csv
open_acl
run_body paso12 "$HERE/no_se_body.py"
