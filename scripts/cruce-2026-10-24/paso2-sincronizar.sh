#!/usr/bin/env bash
# Step 2 of the 24/10/2026 cross: read-only sync of Odoo Online into the lab
# (scripts/online/sincronizar-online.sh, unchanged): new customers, the
# history of each customer and the mirror of Online's payments. Never writes
# to Online. That script commits by default; here it follows the DO_COMMIT
# rule of the other steps.
#   scripts/cruce-2026-10-24/paso2-sincronizar.sh [base]   default: distri_lab
#   DO_COMMIT=1 ...   writes; otherwise applies and rolls back (SOLO_PROBAR=1)
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
LOG=$(log_file paso2)
SOLO=1; [ "$COMMIT" = 1 ] && SOLO=0
(umask 077; SOLO_PROBAR=$SOLO "$HERE/../online/sincronizar-online.sh" "$DB" | tee "$LOG")
echo "log: $LOG"
rg -q '^COMMITTED|^NOT COMMITTED' "$LOG"
