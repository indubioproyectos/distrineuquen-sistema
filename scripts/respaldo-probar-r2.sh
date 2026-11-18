#!/usr/bin/env bash
# Restore test of the off-site copy in Cloudflare R2 (weekly, Sunday 05:45,
# after the 05:15 hourly run that prunes R2).
#
#   scripts/respaldo-probar-r2.sh [<R2 snapshot id>]   default: the newest
#
# Downloads one snapshot of the R2 restic repository (what respaldo-hora.sh
# sends there: distri_lab.dump + filestore.tar) into a throwaway folder on
# this disk, then checks it the way respaldo-probar.sh checks a local backup:
# - restic restore --verify: every restored file matches the repository;
# - the dump and the filestore archive are byte-identical (sha256) to the
#   same snapshot in the local repository (the R2 snapshot records it as
#   "original");
# - the dump restores into the throwaway database distri_r2_prueba
#   (pg_restore --exit-on-error) and respaldo-cifras.sql runs on it. The
#   hourly snapshot carries no figures recorded at backup time, so they are
#   compared with the newest daily backup (backups-auto/diario/*/cifras.txt):
#   every table must exist and the main counts cannot be lower than 90 % of it;
# - the filestore archive extracts completely (file count and bytes equal
#   the archive listing) and every attachment of the database has its file;
# - Odoo opens the copy (scripts/l10n_ar/snap.py, read only).
# The database copy, its filestore and the downloaded files are always
# deleted at the end. Production (distri_lab and its filestore) is never
# written: only read-only restic access and the throwaway names are used.
#
# Result: backups-auto/prueba-restauracion-r2.txt, logs/respaldo.log, and the
# keys r2_restore_checked (last attempt), r2_restore_ok (last success) and
# r2_restore_error (empty when it passed) in run/respaldo-estado.env. On
# failure: logger -p user.err and a line in backups-auto/ALERTA.txt.
# Shares the maintenance lock with the backups and the local restore test.
set -euo pipefail
umask 077

LAB=/srv/odoo-lab
DEST=${DEST:-$LAB/backups-auto}
COPY=distri_r2_prueba
LOCK=$LAB/run/mantenimiento.lock
LOG=$LAB/logs/respaldo.log
STATUS=$LAB/run/respaldo-estado.env
RESULT=$DEST/prueba-restauracion-r2.txt
WORK=/srv/respaldos/.prueba-r2
MIN_FREE_GB=${MIN_FREE_GB:-5}
CONF=$HOME/.config/indubio
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/respaldo-prueba-comun.sh"   # drop_copy, restore_dump, copy_figures, ...
DB=$PROD_DB
SNAPDIR=/srv/respaldos/.en-curso      # the path respaldo-hora.sh backs up

mkdir -p "$LAB/run" "$LAB/logs"
touch "$LOG" && chmod 600 "$LOG"
exec > >(while IFS= read -r line; do printf '%s %s\n' "$(date '+%F %T')" "$line"; done | tee -a "$LOG") 2>&1

exec 9>"$LOCK"
flock -w 3600 9 || { echo "ERROR: candado de mantenimiento ocupado; no se probó la copia de R2"; exit 3; }

# --- repositories: local (as respaldo-hora.sh) and R2 (from r2.env, never printed)
[ -f "$CONF/respaldo.env" ] && . "$CONF/respaldo.env"
LOCAL_REPO=${RESTIC_REPOSITORY:-/srv/respaldos/restic-distri}
export RESTIC_PASSWORD_FILE=$CONF/restic-pass
[ -f "$CONF/r2.env" ] || { echo "ERROR: falta $CONF/r2.env"; exit 1; }
set -a; . "$CONF/r2.env"; set +a
export RESTIC_REPOSITORY="s3:$R2_ENDPOINT/${R2_BUCKET:-distri-respaldos}"
r2() { restic --no-cache -q "$@"; }            # the R2 repository
loc() { RESTIC_REPOSITORY=$LOCAL_REPO restic -q "$@"; }

START=$(date +%s)
FAILS=0 SNAP=? ERR= STEP=inicio
fail() { echo "FALLA: $*"; FAILS=$((FAILS + 1)); ERR=${ERR:-$*}; }
free_kb() { df -Pk "$1" | awk 'NR == 2 {print $4}'; }

set_status() {  # set_status KEY VALUE: run/respaldo-estado.env (host side only)
  touch "$STATUS" && { { grep -v "^$1=" "$STATUS" || true; echo "$1=$2"; } > "$STATUS.tmp" \
    && mv "$STATUS.tmp" "$STATUS"; }
}

# disk at its fullest during the run: sample the free space every 2 s
SAMPLES=$(mktemp) SAMPLER=
( while :; do free_kb /srv/respaldos; sleep 2; done ) > "$SAMPLES" &
SAMPLER=$!

finish() {
  local rc=$? now secs min_kb used_mb
  trap - EXIT
  set +e   # the cleanup and the record must all run, whatever fails here
  drop_copy
  rm -rf "$WORK"
  [ -n "$SAMPLER" ] && kill "$SAMPLER" 2>/dev/null || true
  min_kb=$(sort -n "$SAMPLES" | head -1); rm -f "$SAMPLES"
  used_mb=$(( (${BASE_KB:-$min_kb} - ${min_kb:-0}) / 1024 ))
  now=$(date -Is); secs=$(( $(date +%s) - START ))
  set_status r2_restore_checked "$now" || true
  if [ $rc -eq 0 ] && [ $FAILS -eq 0 ]; then
    set_status r2_restore_ok "$now" || true
    set_status r2_restore_error "" || true
    echo "== prueba de restauración desde R2 OK (snapshot $SNAP, ${secs} s, disco usado en el pico ~${used_mb} MB)"
  else
    [ -n "$ERR" ] || ERR="paso «$STEP» terminó con código $rc"
    set_status r2_restore_error "$now prueba desde R2 falló: ${ERR//$'\n'/ } (ver logs/respaldo.log)" || true
    echo "== prueba de restauración desde R2 FALLÓ ($FAILS diferencias, código $rc, ${secs} s)"
    logger -p user.err -t respaldo-lab "prueba de restauración desde R2 FALLÓ: snapshot $SNAP: $ERR" 2>/dev/null || true
    printf '%s prueba de restauración desde R2 FALLÓ: snapshot %s\n' "$(date '+%F %T')" "$SNAP" >> "$DEST/ALERTA.txt"
    chmod 600 "$DEST/ALERTA.txt" 2>/dev/null || true
  fi
  {
    echo "fecha: $(date '+%F %T %Z')"
    echo "snapshot_r2: $SNAP"
    echo "duracion_s: $secs"
    echo "disco_pico_mb: $used_mb"
    [ -z "$ADDONS_WARN" ] || echo "aviso: $ADDONS_WARN"
    echo "resultado: $([ $rc -eq 0 ] && [ $FAILS -eq 0 ] && echo OK || echo "FALLÓ ($FAILS, código $rc): $ERR")"
  } > "$RESULT"
  chmod 600 "$RESULT"
  exit $(( rc != 0 ? rc : FAILS > 0 ))
}
trap finish EXIT

# leftovers of a run that died
drop_copy
rm -rf "$WORK"
BASE_KB=$(free_kb /srv/respaldos)

# --- 1. the snapshot
STEP="lista de snapshots"
SNAP=${1:-latest}
read -r SNAP ORIG _ < <(r2 snapshots --host distri-prod --json "$SNAP" | python3 -I -c '
import json, sys
s = max(json.load(sys.stdin), key=lambda x: x["time"])
print(s["short_id"], s.get("original") or "-", s["time"])' ) ||
  { ERR="no se pudo leer la lista de snapshots de R2"; exit 1; }
echo "== prueba de restauración desde R2, snapshot $SNAP"

# --- 2. disk guard: 3x the snapshot (download + filestore copy + database) plus MIN_FREE_GB
STEP="espacio en disco"
size=$(r2 stats --mode restore-size --json "$SNAP" | grep -oP '"total_size":\K[0-9]+')
need_kb=$(( size * 3 / 1024 ))
have_kb=$(free_kb /srv/respaldos)
if [ $(( have_kb - need_kb )) -lt $(( MIN_FREE_GB * 1024 * 1024 )) ]; then
  ERR="disco: quedan $((have_kb / 1024)) MB libres, la prueba necesita ~$((need_kb / 1024)) MB y el mínimo es ${MIN_FREE_GB} GB"
  echo "ERROR: $ERR; no se descargó nada"
  exit 2
fi
echo "snapshot: $((size / 1048576)) MB; libres: $((have_kb / 1048576)) GB"

# --- 3. download from R2, every file checked against the repository
STEP="descarga de R2"
mkdir -p "$WORK"
t0=$(date +%s)
r2 restore "$SNAP" --target "$WORK" --verify
GOT=$WORK$SNAPDIR
[ -s "$GOT/$DB.dump" ] && [ -s "$GOT/filestore.tar" ] || { ERR="el snapshot no tiene $DB.dump y filestore.tar"; exit 1; }
echo "descargado y verificado: $(du -sm "$WORK" | cut -f1) MB en $(( $(date +%s) - t0 )) s"

# --- 4. byte-identical to the same snapshot in the local repository
STEP="comparación con el repositorio local"
if [ "$ORIG" != - ] && loc cat snapshot "$ORIG" >/dev/null 2>&1; then
  for f in "$DB.dump" filestore.tar; do
    want=$(loc dump "$ORIG" "$SNAPDIR/$f" | sha256sum | cut -d' ' -f1)
    [ "$(sha256sum "$GOT/$f" | cut -d' ' -f1)" = "$want" ] || fail "$f: sha256 distinto del repositorio local"
  done
  [ $FAILS -eq 0 ] && echo "idéntico al snapshot local ${ORIG:0:8}: $DB.dump y filestore.tar (sha256)"
else
  echo "aviso: el snapshot local de origen ya no está; no se comparó el sha256"
fi

# --- 5. database
STEP="restauración de la base"
restore_dump "$GOT/$DB.dump"
copy_figures > "$WORK/cifras-r2.txt"
REF=$(find "$DEST/diario" -mindepth 2 -maxdepth 2 -name cifras.txt | sort | tail -1)
tables=$(grep -c '^tabla:' "$WORK/cifras-r2.txt")
if [ -z "$REF" ]; then
  echo "aviso: no hay respaldo diario para comparar cifras"
else
  missing=$(comm -23 <(grep -oP '^tabla:[^|]+' "$REF" | sort) <(grep -oP '^tabla:[^|]+' "$WORK/cifras-r2.txt" | sort))
  [ -z "$missing" ] || fail "faltan tablas respecto de $(basename "$(dirname "$REF")"): $(echo $missing | head -c 200)"
  for k in contactos_total facturas_publicadas asientos_total pagos pedidos productos adjuntos historial; do
    want=$(grep -oP "^$k\|\K[0-9]+" "$REF" || echo 0)
    got=$(grep -oP "^$k\|\K[0-9]+" "$WORK/cifras-r2.txt" || echo 0)
    [ $(( got * 10 )) -ge $(( want * 9 )) ] || fail "$k: $got, el respaldo diario tenía $want"
  done
  echo "base: $tables tablas; contactos $(grep -oP '^contactos_total\|\K[0-9]+' "$WORK/cifras-r2.txt"), asientos $(grep -oP '^asientos_total\|\K[0-9]+' "$WORK/cifras-r2.txt"), adjuntos $(grep -oP '^adjuntos\|\K[0-9]+' "$WORK/cifras-r2.txt") (respaldo diario $(basename "$(dirname "$REF")"): coherente)"
fi

# --- 6. filestore
STEP="filestore"
read -r want_n want_b < <(tar tvf "$GOT/filestore.tar" | awk '$1 ~ /^-/ {n++; s += $3} END {print n + 0, s + 0}')
load_filestore xf < "$GOT/filestore.tar"
rm -f "$GOT/filestore.tar"       # its copy is in the volume now: less disk at the peak
read -r n bytes < <(filestore_count)
if [ "$n" = "$want_n" ] && [ "$bytes" = "$want_b" ]; then
  echo "filestore: $n archivos, $bytes bytes: idéntico al archivo"
else
  fail "filestore: $n archivos / $bytes bytes, el archivo tiene $want_n / $want_b"
fi
lost=$(docker exec -i "$DB_CONTAINER" psql -U odoo -d "$COPY" -qAtX -v ON_ERROR_STOP=1 \
  -c "select distinct store_fname from ir_attachment where store_fname is not null" |
  docker exec -i "$ODOO_CONTAINER" bash -c \
  "cd /var/lib/odoo/filestore/$COPY && n=0; while read -r f; do [ -f \"\$f\" ] || n=\$((n+1)); done; echo \$n")
[ "$lost" = 0 ] && echo "adjuntos: todos tienen su archivo" || fail "adjuntos: $lost sin archivo en el filestore"

# --- 7. Odoo opens the copy (read only)
STEP="Odoo abre la copia"
if [ "${SIN_ODOO:-0}" != 1 ]; then
  copy_addons
  [ -z "$ADDONS_WARN" ] || echo "AVISO: $ADDONS_WARN"
  if out=$(odoo_opens prueba-r2); then
    echo "Odoo abre la copia ($ADDONS_NOTE): $out"
  else
    fail "Odoo no pudo abrir la copia (snap.py)"
  fi
fi
