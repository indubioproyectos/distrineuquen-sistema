#!/usr/bin/env bash
# Step 5 of the 24/10/2026 cross: the old period (01/04/2025 to 17/03/2026).
# Loads into the board only the Mercado Pago incomes of carga_viejo.csv (13
# verified by hand) and confirms them with cargar_mp_body.py, each with a note
# naming its origin. Needs steps 1 to 3 committed.
#   scripts/cruce-2026-10-24/paso5-periodo-viejo.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
lab_lock
new_tmp
private_copy "$CRUCE/carga_viejo.csv" carga_final.csv
private_copy "$CRUCE/cruce_final.csv" cruce_final.csv
private_copy "$CRUCE/mp_unificado.csv" mp_unificado.csv
for f in "$INSUMOS/mp/REPORTES MERCADO PAGO/ORIGINALES/"*.xlsx \
         "$INSUMOS"/mp-2026-10-23/*.xlsx "$INSUMOS"/mp-2026-10-24/*.xlsx; do
  private_copy "$f" "mp/$(basename "$f")"
done
mkdir -p "$TMP/img"
ZIPS_ADMIN=("$INSUMOS"/whatsapp-2026-10-24/*.zip)
ZIPS_GERENTE=("$INSUMOS"/whatsapp-gerente-2026-10-24/*.zip)
python3 -I -B "$HERE/extraer_comprobantes.py" "$TMP/carga_final.csv" "$TMP/cruce_final.csv" \
  "${ZIPS_ADMIN[0]}" "${ZIPS_GERENTE[0]}" "$TMP/img"
open_acl
run_body paso5 "$HERE/cargar_mp_body.py" \
  ", CRUCE_DESDE='2025-04-01', CRUCE_HASTA='2026-03-17', SOLO_OPERACIONES_DEL_CRUCE='1', NOTA_CORRIDA='cruce del período viejo (abr-2025 a mar-2026) del 24/10/2026'"
