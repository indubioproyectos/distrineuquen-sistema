#!/usr/bin/env bash
# Step 3 of the 24/10/2026 cross: loads into the collections board the
# Mercado Pago incomes from 18/03/2026 to 24/10/2026, with the module's own
# importer and the ORIGINAL reports, then applies carga_final.csv
# (cargar_mp_body.py). Needs steps 1 and 2 committed.
#   scripts/cruce-2026-10-24/paso3-cargar-mp.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
#   SIMULAR_LIMPIEZA=1    test only (never with DO_COMMIT=1): runs the step 1
#                         cleanup in the same transaction first, so the test
#                         sees an empty board
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
SIM=${SIMULAR_LIMPIEZA:-0}
if [ "$SIM" = 1 ] && [ "$COMMIT" = 1 ]; then
  echo "SIMULAR_LIMPIEZA=1 es solo para probar: corré antes el paso 1 con DO_COMMIT=1" >&2; exit 1
fi
lab_lock
new_tmp
private_copy "$CRUCE/carga_final.csv" carga_final.csv
private_copy "$CRUCE/cruce_final.csv" cruce_final.csv
private_copy "$CRUCE/mp_unificado.csv" mp_unificado.csv
for f in "$INSUMOS/mp/REPORTES MERCADO PAGO/ORIGINALES/"*.xlsx \
         "$INSUMOS"/mp-2026-10-23/*.xlsx "$INSUMOS"/mp-2026-10-24/*.xlsx; do
  private_copy "$f" "mp/$(basename "$f")"
done
mkdir -p "$TMP/img"
ZIPS_ADMIN=("$INSUMOS"/whatsapp-2026-10-24/*.zip)
ZIPS_GERENTE=("$INSUMOS"/whatsapp-gerente-2026-10-24/*.zip)
[ ${#ZIPS_ADMIN[@]} = 1 ] && [ ${#ZIPS_GERENTE[@]} = 1 ] || { echo "se espera un .zip por export" >&2; exit 1; }
python3 -I -B "$HERE/extraer_comprobantes.py" "$TMP/carga_final.csv" "$TMP/cruce_final.csv" \
  "${ZIPS_ADMIN[0]}" "${ZIPS_GERENTE[0]}" "$TMP/img"
install -m 644 "$HERE/../online/limpiar_tablero_body.py" "$TMP/limpiar_tablero_body.py"
open_acl
run_body paso3 "$HERE/cargar_mp_body.py" ", SIMULAR_LIMPIEZA='$SIM'"
