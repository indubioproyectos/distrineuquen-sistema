#!/usr/bin/env bash
# Step 4 of the 24/10/2026 cross: the cash payments into the lab
# (cargar_efectivo_body.py): the 140 payments of the paper sheets
# (cobranza/efectivo/carga_online/cargas.jsonl, memos.jsonl; removed from
# Online on 24/10) and the cash reported in the WhatsApp group that is not in
# Online (efectivo_chat.csv). Needs step 2 committed.
#   scripts/cruce-2026-10-24/paso4-cargar-efectivo.sh [base]   default: distri_lab
#   DO_COMMIT=1 ...   writes; otherwise rolls back
set -euo pipefail
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/comun.sh"
EFECTIVO=/srv/docs/distrineuquen-modulo/cobranza/efectivo/carga_online
lab_lock
new_tmp
private_copy "$EFECTIVO/cargas.jsonl" cargas.jsonl
private_copy "$EFECTIVO/memos.jsonl" memos.jsonl
private_copy "$CRUCE/efectivo_chat.csv" efectivo_chat.csv
open_acl
run_body paso4 "$HERE/cargar_efectivo_body.py"
