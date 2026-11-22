#!/usr/bin/env bash
# Read-only sync of Odoo Online into a lab database: downloads Online with the
# read-only guard (leer_online.py) and applies it (aplicar_online_body.py).
# Never writes to Online.
#   scripts/online/sincronizar-online.sh [base]          default: distri_lab
#   SOLO_PROBAR=1 ...   applies and rolls back (shows what it would do)
#   CONFIRMO=1 ...      required to write
set -euo pipefail
DB=${1:-distri_lab}
COMMIT=1; [ "${SOLO_PROBAR:-0}" = 1 ] && COMMIT=0
# writing needs an explicit CONFIRMO=1: since the 19/11 go-live Online is
# history only and the audited balances must not move by accident
if [ "$COMMIT" = 1 ] && [ "${CONFIRMO:-0}" != 1 ]; then
  echo "ERROR: sincronizar escribe en $DB. Para probar: SOLO_PROBAR=1. Para escribir: CONFIRMO=1" >&2
  exit 1
fi
HERE=$(cd "$(dirname "$0")" && pwd)
INSUMOS=/srv/docs/distrineuquen-modulo/insumos
LAB_DB="$DB" python3 "$HERE/leer_online.py" >/dev/null
DIA=$(date +%F)
# the container user (uid 100) reads the payload through an ACL on a private
# copy, removed at the end; the originals stay 600
TMP=$(mktemp -d /srv/odoo-lab/run/online-XXXXXX)
trap 'rm -rf "$TMP"' EXIT
install -m 600 "$INSUMOS/online/$DIA/payload.json" "$TMP/payload.json"
setfacl -m u:100:rx "$TMP"; setfacl -m u:100:r "$TMP/payload.json"
LAB_MOUNTS="-v $TMP:/online:ro" \
  NAME=distri-online-sync-$$ DO_COMMIT=$COMMIT \
  "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http --log-level=warn \
  < <(echo "import os; os.environ['PAYLOAD_PATH'] = '/online/payload.json'; os.environ['DO_COMMIT'] = '$COMMIT'"; \
      cat "$HERE/aplicar_online_body.py") 2>&1 | grep -E '^(RESULT|COMMITTED|NOT COMMITTED)|Traceback|Error'
