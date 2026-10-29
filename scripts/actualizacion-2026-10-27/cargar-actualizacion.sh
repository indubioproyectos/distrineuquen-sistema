#!/usr/bin/env bash
# Update of 27/10/2026: the receipts of ale's export of the managers' group
# (from 23/10) to the migration board, verified with the MP settlement of 27/10
# (cargar_actualizacion_body.py says what it does and what it checks).
#   scripts/actualizacion-2026-10-27/cargar-actualizacion.sh [base]   default: distri_lab
#   DO_COMMIT=1 ...   writes: first a backup in backups-db/, then the full
#                     read-only sync of Online (scripts/online), then the load
#   otherwise (dry run): reads Online (read only), applies its payments mirror,
#                     the report and the chat in one transaction and rolls back
# Private inputs never enter the repository: they are copied (600) into a
# temporary folder the container user (uid 100) reads through an ACL, mounted
# read only and removed at the end. The detailed CSV (600) and the log go to
# insumos/actualizacion-2026-10-27/.
set -euo pipefail
umask 077
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
LABDIR=/srv/odoo-lab
INSUMOS=/srv/docs/distrineuquen-modulo/insumos
DIR=$INSUMOS/actualizacion-2026-10-27
ZIP="Chat de WhatsApp con Comprobantes de Transferencias - Alejandro.zip"
MP=settlement_v2-571930919-manual-2026-10-27-104746.xlsx
COMMIT=0; [ "${DO_COMMIT:-0}" = 1 ] && COMMIT=1
MODE=$([ "$COMMIT" = 1 ] && echo commit || echo prueba)
STAMP=$(date +%Y%m%d-%H%M%S)
[ -f "$DIR/$ZIP" ] && [ -f "$DIR/$MP" ] || { echo "faltan los insumos en $DIR" >&2; exit 1; }

if [ "$COMMIT" = 1 ]; then
  # the backup takes the maintenance lock itself: before ours
  BK=$LABDIR/backups-db/$(date +%F)-antes-actualizacion-whatsapp-$(date +%H%M)
  echo "respaldo: $BK"
  DEST="$BK" DB="$DB" "$LABDIR/scripts/respaldo-diario.sh" >/dev/null
  ls "$BK"/diario/*/manifiesto.txt >/dev/null 2>&1 || { echo "ABORT: no quedó el respaldo en $BK" >&2; exit 1; }
  # full read-only sync of Online (commits only when every balance matches)
  "$LABDIR/scripts/online/sincronizar-online.sh" "$DB" | tee /dev/stderr | rg '^COMMITTED' >/dev/null \
    || { echo "ABORT: la sincronización con Online no se escribió" >&2; exit 1; }
else
  # fresh read of Online, read only (insumos/online/<today>/payload.json)
  LAB_DB="$DB" python3 -I "$LABDIR/scripts/online/leer_online.py" >/dev/null
fi

exec 9>"$LABDIR/run/mantenimiento.lock"
flock -w 3600 9
TMP=$(mktemp -d "$LABDIR/run/actualizacion-XXXXXX")
trap 'rm -rf "$TMP"' EXIT
install -m 600 "$DIR/$ZIP" "$TMP/$ZIP"
install -m 600 "$DIR/$MP" "$TMP/$MP"
# the chats of the earlier exports (text only): the manager's cash notices
mkdir -p "$TMP/chats"
install -m 600 "$INSUMOS"/whatsapp-2026-10-23/*/*.txt "$TMP/chats/admin-2026-10-23.txt"
unzip -p "$(ls "$INSUMOS"/whatsapp-2026-10-24/*.zip)" '*.txt' > "$TMP/chats/admin-2026-10-24.txt"
unzip -p "$(ls "$INSUMOS"/whatsapp-gerente-2026-10-24/*.zip)" '*.txt' > "$TMP/chats/gerente-2026-10-24.txt"
SIM=0
if [ "$COMMIT" = 0 ]; then
  install -m 600 "$INSUMOS/online/$(date +%F)/payload.json" "$TMP/payload.json"
  SIM=1
fi
setfacl -R -m u:100:rX "$TMP"

OUT=$TMP/salida.txt
LAB_MOUNTS="-v $TMP:/in:ro" NAME=distri-actualizacion-$$ DO_COMMIT=$COMMIT \
  "$LABDIR/scripts/l10n_ar/lab.sh" odoo shell -d "$DB" --no-http --log-level=warn \
  < <(printf '%s\n' "import os; os.environ.update(IN_DIR='/in', DO_COMMIT='$COMMIT', SIMULAR_ONLINE='$SIM', ZIP_NAME=$(python3 -I -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$ZIP"), MP_NAME='$MP')"; \
      cat "$HERE/cargar_actualizacion_body.py") > "$OUT" 2>&1 || true

mkdir -p "$DIR/carga-lab"; chmod 700 "$DIR/carga-lab"
LOG=$DIR/carga-lab/actualizacion-$STAMP-$MODE.log
rg -N -v '^ROW ' "$OUT" > "$LOG" || true
CSV=$DIR/comprobantes-$STAMP-$MODE.csv
rg -N '^ROW ' "$OUT" | python3 -I -c '
import csv, json, sys
rows = [json.loads(line[4:]) for line in sys.stdin]
cols = []
for r in rows:
    cols += [k for k in r if k not in cols]
w = csv.DictWriter(open(sys.argv[1], "w", newline="", encoding="utf-8"), fieldnames=cols)
w.writeheader(); w.writerows(rows)
' "$CSV"
chmod 600 "$LOG" "$CSV"
rg -N -e '^(RESULT|SKIP|WARN|CASH|ABORT|COMMITTED|NOT COMMITTED)' -e 'Traceback' -e 'Error' "$OUT" || true
echo "csv: $CSV"
echo "log: $LOG"
rg -q '^(COMMITTED|NOT COMMITTED)' "$OUT"
