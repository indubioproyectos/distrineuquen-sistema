#!/usr/bin/env bash
# Step 6 of the 24/10/2026 cross: the corrections of the audit
# (auditoria/1_metodo.md, correcciones_body.py): lab payments already in
# Online cancelled, payments moved to the right customer, the board cards the
# cross matched to Online closed (and the «revisar» ones the module closed by
# amount reopened), and the lab copies of the Online drafts deleted on 24/10
# removed. Needs steps 1 to 5 committed.
#   scripts/cruce-2026-10-24/paso6-correcciones.sh [base]    default: distri_lab
#   DO_COMMIT=1 ...       writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
EFECTIVO=/srv/docs/distrineuquen-modulo/cobranza/efectivo/carga_online
lab_lock
new_tmp
private_copy "$CRUCE/online/creditos.json" creditos.json
private_copy "$CRUCE/online/facturas.json" facturas.json
private_copy "$CRUCE/auditoria/1_metodo_casos.csv" 1_metodo_casos.csv
private_copy "$CRUCE/cargado-20261024/online_mp.csv" online_mp.csv
private_copy "$CRUCE/cargado-20261024/online_sin_mp_resuelto.csv" online_sin_mp_resuelto.csv
private_copy "$EFECTIVO/cargas.jsonl" cargas.jsonl
open_acl
run_body paso6 "$HERE/correcciones_body.py"
