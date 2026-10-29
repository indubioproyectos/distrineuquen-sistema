#!/usr/bin/env bash
# Applies the verified correction plan of the WhatsApp receipts to a lab
# database (aplicar_correcciones_body.py). Rolls back unless DO_COMMIT=1.
#   scripts/correcciones/aplicar-correcciones.sh [pasos] [base]
#     pasos: 1 (plan fase 1, default) | 2 (August report + its receipts) | 1,2
#     base:  distri_lab by default
#   DO_COMMIT=1 ...   writes
#   PLAN=... MP_AGOSTO=...   other inputs (defaults below)
set -euo pipefail
PASOS=${1:-1}
DB=${2:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
PLAN=${PLAN:-/tmp/claude-1000/-srv/733ecd0c-b5ea-431a-9f14-e9c55a7ad70d/scratchpad/diag/plan_fase1.json}
# always ORIGINALES: the per-year copies are unreliable (verificacion_plan_2026-10-23.md)
MP_AGOSTO=${MP_AGOSTO:-"/srv/docs/distrineuquen-modulo/insumos/mp/REPORTES MERCADO PAGO/ORIGINALES/settlement_v2-571930919-manual-2026-09-28-120026.xlsx"}
COMMIT=0; [ "${DO_COMMIT:-0}" = 1 ] && COMMIT=1
AGOSTO=$(basename "$MP_AGOSTO")
# the container user (uid 100) reads the inputs through an ACL on a private
# copy, removed at the end; the originals stay 600
TMP=$(mktemp -d /srv/odoo-lab/run/correcciones-XXXXXX)
trap 'rm -rf "$TMP"' EXIT
install -m 600 "$PLAN" "$TMP/plan.json"
install -m 600 "$MP_AGOSTO" "$TMP/$AGOSTO"
setfacl -m u:100:rx "$TMP"; setfacl -m u:100:r "$TMP/plan.json" "$TMP/$AGOSTO"
LAB_MOUNTS="-v $TMP:/correcciones:ro" \
  NAME=distri-correcciones-$$ DO_COMMIT=$COMMIT \
  "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http --log-level=warn \
  < <(printf '%s\n' "import os; os.environ.update(PLAN_PATH='/correcciones/plan.json', MP_AGOSTO_PATH='/correcciones/$AGOSTO', PASOS='$PASOS', DO_COMMIT='$COMMIT')"; \
      printf '%s\n' "$(<"$HERE/aplicar_correcciones_body.py")") 2>&1 \
  | rg -e '^(RESULT|COMMITTED|NOT COMMITTED)' -e 'Traceback' -e 'Error'
