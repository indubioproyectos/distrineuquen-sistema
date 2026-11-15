#!/usr/bin/env bash
# Cash sheets («planillas de rendición») into the lab (19.0.1.75.0):
# the 2026 xlsx archive, the pairings to lab payments, the rows of the cash
# loader and the photos. preparar.py builds the input on the host,
# cargar_planillas_body.py writes it in the Odoo shell (both say what they
# load and why). Never writes a payment or a move; never touches Online.
#   scripts/planillas-lab/cargar-planillas.sh [base]   default: distri_lab (dry run)
#   DO_COMMIT=1 ...      first a backup in backups-db/ (SKIP_BACKUP=1 when one was just made)
# The log goes to logs/planillas-<stamp>-<prueba|commit>.log next to this script (600).
set -euo pipefail
umask 077
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
LABDIR=/srv/odoo-lab
COMMIT=0; [ "${DO_COMMIT:-0}" = 1 ] && COMMIT=1
MODE=$([ "$COMMIT" = 1 ] && echo commit || echo prueba)
STAMP=$(date +%Y%m%d-%H%M%S)

if [ "$COMMIT" = 1 ] && [ "${SKIP_BACKUP:-0}" != 1 ]; then
  BK=$LABDIR/backups-db/$(date +%F)-antes-planillas-lab-$(date +%H%M)
  echo "respaldo: $BK"
  DEST="$BK" DB="$DB" "$LABDIR/scripts/respaldo-diario.sh" >/dev/null
  ls "$BK"/diario/*/manifiesto.txt >/dev/null 2>&1 || { echo "ABORT: no quedó el respaldo en $BK" >&2; exit 1; }
fi

exec 9>"$LABDIR/run/mantenimiento.lock"
flock -w 3600 9
TMP=$(mktemp -d "$LABDIR/run/planillas-lab-XXXXXX")
trap 'rm -rf "$TMP"' EXIT
python3 -I "$HERE/preparar.py" "$TMP"
setfacl -R -m u:100:rX "$TMP"

OUT=$TMP/salida.txt
LAB_MOUNTS="-v $TMP:/in:ro" NAME=distri-planillas-lab-$$ DO_COMMIT=$COMMIT \
  "$LABDIR/scripts/l10n_ar/lab.sh" odoo shell -d "$DB" --no-http --log-level=warn \
  < <(printf '%s\n' "import os; os.environ.update(IN_DIR='/in', DO_COMMIT='$COMMIT')"; \
      cat "$HERE/cargar_planillas_body.py") > "$OUT" 2>&1 || true

mkdir -p "$HERE/logs"; chmod 700 "$HERE/logs"
LOG=$HERE/logs/planillas-$STAMP-$MODE.log
rg -N -e '^[A-Z_]+ [{\[]' -e '^(COMMITTED|NOT COMMITTED|ABORT)' -e 'Traceback' -e 'Error' "$OUT" > "$LOG" || true
chmod 600 "$LOG"
rg -N -e '^(RESULT|PAREOS|CARGA|MEMO|FOTOS|OMITIDO|FACTURA_SIN_CLIENTE|FACTURA_OTRO_NOMBRE|ABORT|COMMITTED|NOT COMMITTED)' -e 'Traceback' -e 'Error' "$OUT" || true
echo "log: $LOG"
rg -q '^(COMMITTED|NOT COMMITTED)' "$OUT"
