#!/usr/bin/env bash
# Cash of the paper sheets of 22-27/10/2026 into the lab's «Efectivo» (CSH1),
# reviewed by ale on 28/10 (cargar_efectivo_body.py says what it loads and
# what it checks). Input: insumos/planillas-2026-10-28/filas.jsonl and the
# fresh read-only read of Online (insumos/online/<ONLINE_DIA>/pagos.json).
#   scripts/planillas-2026-10-28/cargar-efectivo.sh [base]   default: distri_lab (dry run)
#   DO_COMMIT=1 ...   first a backup in backups-db/, then the load
#   FILAS=<file.jsonl> ...   another input file (default: filas.jsonl)
# The log (LOG/SKIP/WARN/CREDITO/RESULT) and the created ids go to
# insumos/planillas-2026-10-28/carga-lab/ (600). Undo: deshacer-efectivo.sh.
set -euo pipefail
umask 077
DB=${1:-distri_lab}
HERE=$(cd "$(dirname "$0")" && pwd)
LABDIR=/srv/odoo-lab
DIR=/srv/docs/distrineuquen-modulo/insumos/planillas-2026-10-28
FILAS=${FILAS:-$DIR/filas.jsonl}
ONLINE=/srv/docs/distrineuquen-modulo/insumos/online/${ONLINE_DIA:-$(date +%F)}
COMMIT=0; [ "${DO_COMMIT:-0}" = 1 ] && COMMIT=1
MODE=$([ "$COMMIT" = 1 ] && echo commit || echo prueba)
STAMP=$(date +%Y%m%d-%H%M%S)
[ -f "$FILAS" ] && [ -f "$ONLINE/pagos.json" ] || { echo "faltan $FILAS o $ONLINE/pagos.json" >&2; exit 1; }

if [ "$COMMIT" = 1 ]; then
  BK=$LABDIR/backups-db/$(date +%F)-antes-planillas-efectivo-$(date +%H%M)
  echo "respaldo: $BK"
  DEST="$BK" DB="$DB" "$LABDIR/scripts/respaldo-diario.sh" >/dev/null
  ls "$BK"/diario/*/manifiesto.txt >/dev/null 2>&1 || { echo "ABORT: no quedó el respaldo en $BK" >&2; exit 1; }
fi

exec 9>"$LABDIR/run/mantenimiento.lock"
flock -w 3600 9
TMP=$(mktemp -d "$LABDIR/run/planillas-XXXXXX")
trap 'rm -rf "$TMP"' EXIT
install -m 600 "$FILAS" "$TMP/filas.jsonl"
install -m 600 "$ONLINE/pagos.json" "$TMP/pagos.json"
setfacl -R -m u:100:rX "$TMP"

OUT=$TMP/salida.txt
LAB_MOUNTS="-v $TMP:/in:ro" NAME=distri-planillas-$$ DO_COMMIT=$COMMIT \
  "$LABDIR/scripts/l10n_ar/lab.sh" odoo shell -d "$DB" --no-http --log-level=warn \
  < <(printf '%s\n' "import os; os.environ.update(IN_DIR='/in', DO_COMMIT='$COMMIT')"; \
      cat "$HERE/cargar_efectivo_body.py") > "$OUT" 2>&1 || true

mkdir -p "$DIR/carga-lab"; chmod 700 "$DIR/carga-lab"
LOG=$DIR/carga-lab/efectivo-$STAMP-$MODE.log
rg -N -e '^(LOG|APLICA|RESULT|SKIP|WARN|CREDITO|ABORT|COMMITTED|NOT COMMITTED)' -e 'Traceback' -e 'Error' "$OUT" > "$LOG" || true
if [ "$COMMIT" = 1 ] && rg -q '^COMMITTED' "$OUT"; then
  rg -N '^LOG ' "$OUT" | python3 -I -c 'import json,sys; [print(json.loads(l[4:])["pago"]) for l in sys.stdin]' \
    > "$DIR/carga-lab/pagos-creados-$STAMP.txt"
  echo "ids: $DIR/carga-lab/pagos-creados-$STAMP.txt"
fi
chmod 600 "$DIR"/carga-lab/*
rg -N -e '^(APLICA|RESULT|SKIP|WARN|CREDITO|ABORT|COMMITTED|NOT COMMITTED)' -e 'Traceback' -e 'Error' "$OUT" || true
echo "log: $LOG"
rg -q '^(COMMITTED|NOT COMMITTED)' "$OUT"
