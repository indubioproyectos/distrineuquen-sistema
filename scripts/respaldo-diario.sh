#!/usr/bin/env bash
# Automatic backup of the lab (distri_lab): database dump + filestore.
#
#   scripts/respaldo-diario.sh            the daily run (cron, 04:00 Argentina)
#
# - Consistent: the key figures (respaldo-cifras.sql) and pg_dump read the same
#   exported snapshot, so the restore test can compare them exactly.
# - Atomic: everything is written to backups-auto/.en-curso-*, checked, and
#   only then renamed to diario/<date>. A failed run leaves no valid backup.
# - Rotation: 7 daily, 4 weekly (Sunday) and 3 monthly (the 1st). Weekly and
#   monthly copies are hardlinks to the daily one: no extra space.
# - Disk guard: never leaves less than MIN_FREE_GB free on /.
# - Shares the maintenance lock with actualizar-modulo.sh and
#   respaldo-probar.sh: never runs during a module upgrade.
# - Nothing leaves this server. copia_fuera_del_servidor() is the hook for
#   an encrypted off-site copy, disabled until it is decided.
set -euo pipefail
umask 077

LAB=/srv/odoo-lab
DEST=${DEST:-$LAB/backups-auto}
DB=${DB:-distri_lab}
DB_CONTAINER=odoo-lab-db-1
ODOO_CONTAINER=odoo-lab-odoo-1
MIN_FREE_GB=${MIN_FREE_GB:-5}
KEEP_DAILY=7 KEEP_WEEKLY=4 KEEP_MONTHLY=3
LOCK=$LAB/run/mantenimiento.lock
LOG=$LAB/logs/respaldo.log
TZ_AR=America/Argentina/Buenos_Aires
HERE=$(cd "$(dirname "$0")" && pwd)

mkdir -p "$LAB/run" "$LAB/logs" "$DEST/diario" "$DEST/semanal" "$DEST/mensual"
chmod 700 "$LAB/run" "$LAB/logs" "$DEST" "$DEST/diario" "$DEST/semanal" "$DEST/mensual"

# --- log, rotated here (5 files of 1 MB)
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 1048576 ]; then
  for i in 4 3 2 1; do [ -f "$LOG.$i" ] && mv -f "$LOG.$i" "$LOG.$((i + 1))"; done
  mv -f "$LOG" "$LOG.1"
fi
touch "$LOG" && chmod 600 "$LOG"
exec > >(while IFS= read -r line; do printf '%s %s\n' "$(date '+%F %T')" "$line"; done | tee -a "$LOG") 2>&1

alert() {
  echo "ERROR: $*"
  logger -p user.err -t respaldo-lab "ERROR: $*" 2>/dev/null || true
  printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$DEST/ALERTA.txt"
  chmod 600 "$DEST/ALERTA.txt"
}

# --- one maintenance task at a time (upgrade, backup, restore test)
exec 9>"$LOCK"
if ! flock -w 3600 9; then
  alert "otra tarea de mantenimiento tiene el candado hace más de una hora; no se respaldó"
  exit 3
fi

TODAY=$(TZ=$TZ_AR date +%F)
DOW=$(TZ=$TZ_AR date +%u)   # 7 = Sunday
DOM=$(TZ=$TZ_AR date +%d)
TMP=$DEST/.en-curso-$TODAY-$$
P_IN= P_OUT= P_PID=

cleanup() {
  local rc=$?
  [ -n "${P_PID:-}" ] && kill "$P_PID" 2>/dev/null || true
  if [ -d "$TMP" ]; then rm -rf "$TMP"; fi
  if [ $rc -ne 0 ]; then alert "el respaldo de $TODAY falló (código $rc); no quedó ningún respaldo a medias"; fi
}
trap cleanup EXIT

echo "== respaldo $DB $TODAY"

# --- disk guard: the last backup's size (or 300 MB) must fit with MIN_FREE_GB left
last=$(find "$DEST/diario" -mindepth 1 -maxdepth 1 -type d | sort | tail -1)
need_kb=$(( 300 * 1024 ))
if [ -n "$last" ]; then need_kb=$(( $(du -sk "$last" | cut -f1) * 12 / 10 )); fi
free_kb=$(df -Pk / | awk 'NR == 2 {print $4}')
if [ $(( free_kb - need_kb )) -lt $(( MIN_FREE_GB * 1024 * 1024 )) ]; then
  alert "disco: quedan $((free_kb / 1024)) MB libres y el respaldo necesita ~$((need_kb / 1024)) MB; el mínimo es ${MIN_FREE_GB} GB. No se escribió nada"
  exit 2
fi

# leftovers of a run that died (power cut, kill -9)
find "$DEST" -mindepth 1 -maxdepth 1 -name '.en-curso-*' -type d -exec rm -rf {} +
mkdir "$TMP"

# --- database: figures and dump read the same snapshot
coproc PSQL { docker exec -i "$DB_CONTAINER" psql -U odoo -d "$DB" -qAtX -v ON_ERROR_STOP=1; }
P_IN=${PSQL[1]} P_OUT=${PSQL[0]} P_PID=$PSQL_PID
echo "begin isolation level repeatable read read only; select pg_export_snapshot();" >&"$P_IN"
read -t 60 -r SNAP <&"$P_OUT" || { echo "no se obtuvo el snapshot"; exit 1; }
{ cat "$HERE/respaldo-cifras.sql"; echo "select '__FIN__';"; } >&"$P_IN"
while read -t 300 -r line <&"$P_OUT"; do
  [ "$line" = "__FIN__" ] && break
  echo "$line"
done > "$TMP/cifras.txt"
tail -1 "$TMP/cifras.txt" | grep -q '^tabla:' || { echo "cifras incompletas"; exit 1; }
docker exec "$DB_CONTAINER" pg_dump -U odoo -Fc --snapshot="$SNAP" "$DB" > "$TMP/$DB.dump"
echo "commit;" >&"$P_IN"
eval "exec $P_IN>&-"
wait "$P_PID" || true
P_PID=
docker exec -i "$DB_CONTAINER" pg_restore --list < "$TMP/$DB.dump" > /dev/null
echo "base: $(du -h "$TMP/$DB.dump" | cut -f1)"

# --- filestore (Odoo keeps running: at 04:00 almost nothing writes attachments)
docker exec "$ODOO_CONTAINER" tar czf - -C /var/lib/odoo/filestore "$DB" > "$TMP/filestore-$DB.tgz"
gzip -t "$TMP/filestore-$DB.tgz"
read -r fs_files fs_bytes < <(tar tzvf "$TMP/filestore-$DB.tgz" |
  awk '$1 ~ /^-/ {n++; s += $3} END {print n + 0, s + 0}')
echo "filestore: $(du -h "$TMP/filestore-$DB.tgz" | cut -f1), $fs_files archivos"

# --- manifest
commit=$(git -C "$LAB" rev-parse --short HEAD 2>/dev/null || echo "sin-git")
dirty=$(git -C "$LAB" status --porcelain -- addons 2>/dev/null | wc -l)
(
  cd "$TMP"
  echo "fecha: $(TZ=$TZ_AR date '+%F %T %Z')"
  echo "base: $DB"
  echo "snapshot: $SNAP"
  echo "modulo_en_base: $(grep '^modulo|' cifras.txt | cut -d'|' -f2)"
  echo "modulo_en_codigo: $(grep -oP '"version":\s*"\K[^"]+' "$LAB/addons/distri_pedido/__manifest__.py")"
  echo "git: $commit$([ "$dirty" -gt 0 ] && echo " (+$dirty cambios sin commit en addons)")"
  echo "filestore_archivos: $fs_files"
  echo "filestore_bytes: $fs_bytes"
  for f in "$DB.dump" "filestore-$DB.tgz" cifras.txt; do
    echo "archivo: $f $(stat -c %s "$f") $(sha256sum "$f" | cut -d' ' -f1)"
  done
) > "$TMP/manifiesto.txt"
chmod 600 "$TMP"/*

# --- publish: rename is atomic on the same filesystem
FINAL=$DEST/diario/$TODAY
if [ -d "$FINAL" ]; then
  mv "$FINAL" "$DEST/.reemplazado-$TODAY-$$"
fi
mv "$TMP" "$FINAL"
rm -rf "$DEST/.reemplazado-$TODAY-$$"
echo "listo: $FINAL ($(du -sh "$FINAL" | cut -f1))"

link_copy() {  # weekly / monthly: hardlinks, no extra space
  local kind=$1 target=$DEST/$1/$TODAY
  [ -d "$target" ] && rm -rf "$target"
  cp -al "$FINAL" "$target.tmp" && mv "$target.tmp" "$target"
  echo "copia $kind: $target"
}
[ "$DOW" = 7 ] && link_copy semanal
[ "$DOM" = 01 ] && link_copy mensual

# --- rotation: the newest N of each kind
rotate() {
  local dir=$DEST/$1 keep=$2
  find "$dir" -mindepth 1 -maxdepth 1 -type d -name '20??-??-??' | sort | head -n -"$keep" |
    while read -r old; do rm -rf "$old"; echo "rotación: borrado $old"; done
}
rotate diario $KEEP_DAILY
rotate semanal $KEEP_WEEKLY
rotate mensual $KEEP_MONTHLY

# --- off-site copy hook (disabled: nothing leaves this server yet)
copia_fuera_del_servidor() {
  # Later: encrypt (age or gpg with a public key kept here) and upload to R2/B2
  # with a key that can only write. Enable with RESPALDO_FUERA=1 and a
  # scripts/respaldo-fuera.sh that receives the folder as $1.
  if [ "${RESPALDO_FUERA:-0}" = 1 ] && [ -x "$HERE/respaldo-fuera.sh" ]; then
    "$HERE/respaldo-fuera.sh" "$1"
  else
    echo "copia fuera del servidor: desactivada"
  fi
}
copia_fuera_del_servidor "$FINAL"

echo "total en backups-auto: $(du -sh "$DEST" | cut -f1) (los enlaces duros cuentan una vez)"
echo "== fin OK"
