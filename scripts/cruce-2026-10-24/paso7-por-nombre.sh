#!/usr/bin/env bash
# Step 7 of the 24/10/2026 cross: bank transfers of the recent period whose
# payer was identified by the name in the MP account statements and verified
# (independent evidence + blind check). Confirms only the operations of
# carga_nombres.csv with cargar_mp_body.py, each with a note naming its origin.
# REABRIR_OPS lists cards the module had closed as «Ya en Online» by amount
# only, reopened first. Needs steps 1 to 6 committed.
#   scripts/cruce-2026-10-24/paso7-por-nombre.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
private_copy "$CRUCE/carga_nombres.csv" carga_final.csv
private_copy "$CRUCE/cruce_final.csv" cruce_final.csv
private_copy "$CRUCE/mp_unificado.csv" mp_unificado.csv
for f in "$INSUMOS/mp/REPORTES MERCADO PAGO/ORIGINALES/"*.xlsx \
         "$INSUMOS"/mp-2026-10-23/*.xlsx "$INSUMOS"/mp-2026-10-24/settlement*.xlsx; do
  private_copy "$f" "mp/$(basename "$f")"
done
mkdir -p "$TMP/img"
REOPEN=$(<"$CRUCE/carga_nombres_reabrir.txt")
open_acl
run_body paso7 "$HERE/cargar_mp_body.py" \
  ", CRUCE_DESDE='2026-03-18', CRUCE_HASTA='2026-10-24', SOLO_OPERACIONES_DEL_CRUCE='1', REABRIR_OPS='$REOPEN', NOTA_CORRIDA='cruce por nombre del resumen de cuenta de MP del 24/10/2026'"
