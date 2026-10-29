#!/usr/bin/env bash
# Undo of cargar-efectivo.sh: cancels the payments of one load.
#   scripts/planillas-2026-10-28/deshacer-efectivo.sh <pagos-creados-….txt> [base]   dry run
#   DO_COMMIT=1 ...   writes (first a backup in backups-db/)
# The ids file is in insumos/planillas-2026-10-28/carga-lab/.
set -euo pipefail
umask 077
IDS=${1:?falta el archivo pagos-creados-….txt}
DB=${2:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
LABDIR=/srv/odoo-lab
DIR=/srv/docs/distrineuquen-modulo/insumos/planillas-2026-10-28
COMMIT=0; [ "${DO_COMMIT:-0}" = 1 ] && COMMIT=1
MODE=$([ "$COMMIT" = 1 ] && echo commit || echo prueba)
if [ "$COMMIT" = 1 ]; then
  BK=$LABDIR/backups-db/$(date +%F)-antes-deshacer-planillas-$(date +%H%M)
  echo "respaldo: $BK"
  DEST="$BK" DB="$DB" "$LABDIR/scripts/respaldo-diario.sh" >/dev/null
  ls "$BK"/diario/*/manifiesto.txt >/dev/null 2>&1 || { echo "ABORT: no quedó el respaldo en $BK" >&2; exit 1; }
fi
exec 9>"$LABDIR/run/mantenimiento.lock"
flock -w 3600 9
TMP=$(mktemp -d "$LABDIR/run/planillas-XXXXXX")
trap 'rm -rf "$TMP"' EXIT
install -m 600 "$IDS" "$TMP/ids.txt"
setfacl -R -m u:100:rX "$TMP"
OUT=$TMP/salida.txt
LAB_MOUNTS="-v $TMP:/in:ro" NAME=distri-planillas-undo-$$ DO_COMMIT=$COMMIT \
  "$LABDIR/scripts/l10n_ar/lab.sh" odoo shell -d "$DB" --no-http --log-level=warn \
  < <(printf '%s\n' "import os; os.environ.update(IN_DIR='/in', DO_COMMIT='$COMMIT')"; \
      cat "$HERE/deshacer_efectivo_body.py") > "$OUT" 2>&1 || true
mkdir -p "$DIR/carga-lab"; chmod 700 "$DIR/carga-lab"
LOG=$DIR/carga-lab/deshacer-$(date +%Y%m%d-%H%M%S)-$MODE.log
rg -N -e '^(LOG|RESULT|ABORT|COMMITTED|NOT COMMITTED)' -e 'Traceback' -e 'Error' "$OUT" > "$LOG" || true
chmod 600 "$LOG"
rg -N -e '^(RESULT|ABORT|COMMITTED|NOT COMMITTED)' -e 'Traceback' -e 'Error' "$OUT" || true
echo "log: $LOG"
rg -q '^(COMMITTED|NOT COMMITTED)' "$OUT"
