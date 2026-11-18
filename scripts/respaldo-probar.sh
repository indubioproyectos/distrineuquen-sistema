#!/usr/bin/env bash
# Restore test of an automatic backup (weekly, after the Sunday backup).
#
#   scripts/respaldo-probar.sh [backups-auto/diario/<fecha>]   default: the newest
#
# Restores the dump into a throwaway database and the filestore into its
# folder, then checks against what the backup recorded at backup time:
# - every key figure and the rows of every table (respaldo-cifras.sql, same
#   snapshot as the dump): they must be identical;
# - the sha256 of each file, and the filestore file count and bytes;
# - that Odoo opens the copy (scripts/l10n_ar/snap.py, read only).
# The copy is always dropped at the end. The result is written to
# <backup>/prueba-restauracion.txt and to logs/respaldo.log.
set -euo pipefail
umask 077

LAB=/srv/odoo-lab
DEST=${DEST:-$LAB/backups-auto}
DB=distri_lab
COPY=distri_resp_prueba
DB_CONTAINER=odoo-lab-db-1
ODOO_CONTAINER=odoo-lab-odoo-1
LOCK=$LAB/run/mantenimiento.lock
LOG=$LAB/logs/respaldo.log
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/respaldo-prueba-comun.sh"   # drop_copy, restore_dump, copy_figures, ...

mkdir -p "$LAB/run" "$LAB/logs"
touch "$LOG" && chmod 600 "$LOG"
exec > >(while IFS= read -r line; do printf '%s %s\n' "$(date '+%F %T')" "$line"; done | tee -a "$LOG") 2>&1

exec 9>"$LOCK"
flock -w 3600 9 || { echo "ERROR: candado de mantenimiento ocupado; no se probó"; exit 3; }

SRC=${1:-$(find "$DEST/diario" -mindepth 1 -maxdepth 1 -type d | sort | tail -1)}
[ -n "$SRC" ] && [ -f "$SRC/manifiesto.txt" ] || { echo "ERROR: no hay respaldo para probar"; exit 1; }
RESULT=$SRC/prueba-restauracion.txt
FAILS=0
fail() { echo "FALLA: $*"; FAILS=$((FAILS + 1)); }

finish() {
  local rc=$?
  drop_copy
  if [ $rc -ne 0 ] || [ $FAILS -ne 0 ]; then
    echo "== prueba de restauración FALLÓ ($FAILS diferencias, código $rc)"
    logger -p user.err -t respaldo-lab "prueba de restauración FALLÓ: $SRC" 2>/dev/null || true
    printf '%s prueba de restauración FALLÓ: %s\n' "$(date '+%F %T')" "$SRC" >> "$DEST/ALERTA.txt"
  fi
}
trap finish EXIT

echo "== prueba de restauración de $SRC"
drop_copy

# 1. the files are the ones the backup wrote
while read -r _ name size sum; do
  [ "$(stat -c %s "$SRC/$name")" = "$size" ] || fail "$name: tamaño distinto"
  [ "$(sha256sum "$SRC/$name" | cut -d' ' -f1)" = "$sum" ] || fail "$name: sha256 distinto"
done < <(grep '^archivo: ' "$SRC/manifiesto.txt")

# 2. database
restore_dump "$SRC/$DB.dump"
copy_figures > "$SRC/.cifras-restauradas.txt"
if diff -q "$SRC/cifras.txt" "$SRC/.cifras-restauradas.txt" >/dev/null; then
  tables=$(grep -c '^tabla:' "$SRC/cifras.txt")
  echo "cifras y filas de $tables tablas: idénticas"
else
  diff "$SRC/cifras.txt" "$SRC/.cifras-restauradas.txt" | head -20
  fail "cifras distintas"
fi
rm -f "$SRC/.cifras-restauradas.txt"

# 3. filestore
load_filestore xzf < "$SRC/filestore-$DB.tgz"
read -r n bytes < <(filestore_count)
want_n=$(grep '^filestore_archivos: ' "$SRC/manifiesto.txt" | cut -d' ' -f2)
want_b=$(grep '^filestore_bytes: ' "$SRC/manifiesto.txt" | cut -d' ' -f2)
if [ "$n" = "$want_n" ] && [ "$bytes" = "$want_b" ]; then
  echo "filestore: $n archivos, $bytes bytes: idéntico"
else
  fail "filestore: $n archivos / $bytes bytes, esperaba $want_n / $want_b"
fi

# 4. Odoo opens the copy (read only)
if [ "${SIN_ODOO:-0}" != 1 ]; then
  copy_addons
  [ -z "$ADDONS_WARN" ] || echo "AVISO: $ADDONS_WARN"
  if out=$(odoo_opens prueba-restauracion); then
    echo "Odoo abre la copia ($ADDONS_NOTE): $out"
  else
    fail "Odoo no pudo abrir la copia (snap.py)"
  fi
fi

{
  echo "fecha: $(date '+%F %T %Z')"
  echo "respaldo: $SRC"
  [ -z "$ADDONS_WARN" ] || echo "aviso: $ADDONS_WARN"
  echo "resultado: $([ $FAILS -eq 0 ] && echo OK || echo "FALLÓ ($FAILS)")"
} > "$RESULT"
chmod 600 "$RESULT"
[ $FAILS -eq 0 ] && echo "== prueba de restauración OK"
exit $(( FAILS > 0 ))
