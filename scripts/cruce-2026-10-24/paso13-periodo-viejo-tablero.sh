#!/usr/bin/env bash
# Step 13 of the 24/10/2026 cross: every Mercado Pago income of 01/04/2025 to
# 17/03/2026 onto the board, with the cross's decisions (no time cutoff:
# periodo_viejo_tablero_body.py, input paso13_viejo.csv). Nothing is
# confirmed and no balance changes. Needs steps 1 to 12 committed.
#   scripts/cruce-2026-10-24/paso13-periodo-viejo-tablero.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
private_copy "$CRUCE/paso13_viejo.csv" paso13_viejo.csv
for f in "$INSUMOS/mp/REPORTES MERCADO PAGO/ORIGINALES/"*.xlsx \
         "$INSUMOS"/mp-2026-10-23/*.xlsx "$INSUMOS"/mp-2026-10-24/settlement*.xlsx; do
  private_copy "$f" "mp/$(basename "$f")"
done
open_acl
run_body paso13 "$HERE/periodo_viejo_tablero_body.py"
