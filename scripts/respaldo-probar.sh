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

drop_copy() {
  docker exec "$DB_CONTAINER" dropdb -U odoo --if-exists "$COPY" >/dev/null 2>&1 || true
  docker exec "$ODOO_CONTAINER" rm -rf "/var/lib/odoo/filestore/$COPY" || true
}
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
docker exec "$DB_CONTAINER" createdb -U odoo "$COPY"
docker exec -i "$DB_CONTAINER" pg_restore -U odoo -d "$COPY" --no-owner --role=odoo --exit-on-error \
  < "$SRC/$DB.dump"
docker exec -i "$DB_CONTAINER" psql -U odoo -d "$COPY" -qAtX -v ON_ERROR_STOP=1 \
  < "$HERE/respaldo-cifras.sql" > "$SRC/.cifras-restauradas.txt"
if diff -q "$SRC/cifras.txt" "$SRC/.cifras-restauradas.txt" >/dev/null; then
  tables=$(grep -c '^tabla:' "$SRC/cifras.txt")
  echo "cifras y filas de $tables tablas: idénticas"
else
  diff "$SRC/cifras.txt" "$SRC/.cifras-restauradas.txt" | head -20
  fail "cifras distintas"
fi
rm -f "$SRC/.cifras-restauradas.txt"

# 3. filestore
docker exec -i "$ODOO_CONTAINER" bash -c "
  set -e; mkdir -p /tmp/resp-prueba && tar xzf - -C /tmp/resp-prueba &&
  mv /tmp/resp-prueba/$DB /var/lib/odoo/filestore/$COPY && rmdir /tmp/resp-prueba" \
  < "$SRC/filestore-$DB.tgz"
read -r n bytes < <(docker exec "$ODOO_CONTAINER" bash -c \
  "find /var/lib/odoo/filestore/$COPY -type f -printf '%s\n' | awk '{n++; s+=\$1} END {print n+0, s+0}'")
want_n=$(grep '^filestore_archivos: ' "$SRC/manifiesto.txt" | cut -d' ' -f2)
want_b=$(grep '^filestore_bytes: ' "$SRC/manifiesto.txt" | cut -d' ' -f2)
if [ "$n" = "$want_n" ] && [ "$bytes" = "$want_b" ]; then
  echo "filestore: $n archivos, $bytes bytes: idéntico"
else
  fail "filestore: $n archivos / $bytes bytes, esperaba $want_n / $want_b"
fi

# 4. Odoo opens the copy (read only)
if [ "${SIN_ODOO:-0}" != 1 ]; then
  out=$(NAME=resp-prueba-snap SNAP_TAG=prueba-restauracion "$LAB/scripts/l10n_ar/lab.sh" \
    odoo shell -d "$COPY" --no-http --log-level=warn < "$LAB/scripts/l10n_ar/snap.py" 2>&1 |
    grep '^SNAP ' || true)
  rm -f "$LAB/scripts/l10n_ar/out/snap_prueba-restauracion.json" 2>/dev/null || true
  if [ -n "$out" ]; then
    echo "Odoo abre la copia: $(echo "$out" | grep -oP 'PARTNERS \d+ TOTAL \S+')"
  else
    fail "Odoo no pudo abrir la copia (snap.py)"
  fi
fi

{
  echo "fecha: $(date '+%F %T %Z')"
  echo "respaldo: $SRC"
  echo "resultado: $([ $FAILS -eq 0 ] && echo OK || echo "FALLÓ ($FAILS)")"
} > "$RESULT"
chmod 600 "$RESULT"
[ $FAILS -eq 0 ] && echo "== prueba de restauración OK"
exit $(( FAILS > 0 ))
