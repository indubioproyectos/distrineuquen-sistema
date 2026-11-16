#!/usr/bin/env bash
# Hourly backup of DistriNeuquén production (distri_lab on this server,
# served at distrineuquen.com since 15/11/2026): database dump + filestore
# into a restic repository (encrypted, deduplicated: an hour only adds what
# changed). Runs from ale's crontab at minute 15.
#
#   RESTIC_REPOSITORY  default /srv/respaldos/restic-distri (local, this server)
#                      set to s3:https://<account>.r2.cloudflarestorage.com/<bucket>
#                      plus AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in
#                      ~/.config/indubio/respaldo.env to send it off-site.
#   password           ~/.config/indubio/restic-pass (600). Keep a copy OUTSIDE
#                      this server: without it nothing restores.
#   HC_URL             optional Healthchecks.io ping URL (in respaldo.env).
#
# Retention: 24 hourly, 7 daily, 4 weekly, 6 monthly. Shares the maintenance
# lock with actualizar-modulo.sh and respaldo-diario.sh.
#
# Off-site copy (since 15/11/2026): when ~/.config/indubio/r2.env exists
# (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, R2_ENDPOINT, optional R2_BUCKET),
# every run copies the new snapshots to the Cloudflare R2 repository
# (`restic copy`: only new data travels). Once a day, at PRUNE_HOUR, the R2
# repository gets the same retention plus a size check. An R2 failure never
# fails the local backup: it is logged and recorded in STATUS.
# The R2 token is limited to one bucket and to this server's IPv4 and IPv6
# (restic connects over IPv6 when it can).
set -euo pipefail
umask 077

LAB=/srv/odoo-lab
DB=distri_lab
CONF=$HOME/.config/indubio
[ -f "$CONF/respaldo.env" ] && . "$CONF/respaldo.env"
export RESTIC_REPOSITORY=${RESTIC_REPOSITORY:-/srv/respaldos/restic-distri}
export RESTIC_PASSWORD_FILE=$CONF/restic-pass
export AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-} AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-}
STAGE=/srv/respaldos/.en-curso
LOG=$LAB/logs/respaldo-hora.log

exec >>"$LOG" 2>&1
exec 9>"$LAB/run/mantenimiento.lock"
flock -w 900 9 || { echo "$(date -Is) lock busy, skipped"; exit 0; }

ping() { [ -n "${HC_URL:-}" ] && curl -fsS -m 10 --retry 3 "$HC_URL$1" >/dev/null || true; }
trap 'echo "$(date -Is) FAILED"; ping /fail' ERR
echo "$(date -Is) start"; ping /start

rm -rf "$STAGE"; mkdir -p "$STAGE"
docker exec odoo-lab-db-1 pg_dump -U odoo -Fc "$DB" > "$STAGE/$DB.dump"
[ "$(stat -c %s "$STAGE/$DB.dump")" -gt 1000000 ] || { echo "dump too small"; false; }
docker exec odoo-lab-odoo-1 tar -C /var/lib/odoo/filestore -cf - "$DB" > "$STAGE/filestore.tar"

restic cat config >/dev/null 2>&1 || restic init
restic backup --host distri-prod --tag hourly "$STAGE"
KEEP=(--keep-hourly 24 --keep-daily 7 --keep-weekly 4 --keep-monthly 6)
restic forget --host distri-prod --prune "${KEEP[@]}"
rm -rf "$STAGE"
echo "$(date -Is) ok"

# --- off-site copy to Cloudflare R2
STATUS=$LAB/run/respaldo-estado.env   # KEY=VALUE lines (host side)
PRUNE_HOUR=05

# The same values go to Odoo's «Respaldos» card (distri_pedido, model
# distri.backup.status: one row, the lowest id), because the Odoo container
# cannot see run/. Values travel as psql variables (:'v' quoting), never
# inside the SQL text; the column comes from the fixed list in db_status. A
# failed write never fails the backup: it is logged and the run goes on.
PSQL=(docker exec -i odoo-lab-db-1 psql -X -q -U odoo -d "$DB" -v ON_ERROR_STOP=1)
if found=$("${PSQL[@]}" -At -c "SELECT to_regclass('public.distri_backup_status') IS NOT NULL" 2>&1); then
  if [ "$found" = t ]; then DB_STATUS=1; else
    DB_STATUS=0; echo "$(date -Is) Odoo backup status table missing (distri_pedido not upgraded yet), not written"
  fi
else
  DB_STATUS=0; echo "$(date -Is) could not check the Odoo backup status table, not written: $found"
fi
db_status() {  # db_status KEY VALUE; returns psql's exit code
  local expr
  case $1 in
    local_ok|r2_ok|r2_checked) expr="NULLIF(:'v', '')::timestamptz AT TIME ZONE 'UTC'" ;;
    r2_error)                  expr="NULLIF(:'v', '')" ;;
    r2_bytes)                  expr="NULLIF(:'v', '')::numeric" ;;
    r2_snapshots)              expr="NULLIF(:'v', '')::integer" ;;
    *) echo "$(date -Is) unknown status key $1, not written to Odoo"; return 0 ;;
  esac
  # update the row, or create it when there is none yet
  "${PSQL[@]}" -v v="$2" <<SQL
WITH updated AS (
  UPDATE distri_backup_status SET $1 = $expr, write_date = now() AT TIME ZONE 'UTC'
   WHERE id = (SELECT min(id) FROM distri_backup_status)
  RETURNING id)
INSERT INTO distri_backup_status (create_date, write_date, $1)
SELECT now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC', $expr
 WHERE NOT EXISTS (SELECT 1 FROM updated);
SQL
}
set_status() {  # set_status KEY VALUE; returns the env file write's status
  local rc=0
  touch "$STATUS" && { { grep -v "^$1=" "$STATUS" || true; echo "$1=$2"; } > "$STATUS.tmp" \
    && mv "$STATUS.tmp" "$STATUS"; } || rc=$?
  # set_status also runs where set -e is off (inside `if r2_copy`): check explicitly
  if [ "$DB_STATUS" = 1 ]; then
    db_status "$1" "$2" || echo "$(date -Is) writing $1 to the Odoo backup status table failed (backup unaffected)"
  fi
  return "$rc"
}
set_status local_ok "$(date -Is)"

# Runs in a subshell. Called from an `if`, where bash ignores set -e, so every
# step checks its own exit code.
r2_copy() (
  set -a; . "$CONF/r2.env" || exit 1; set +a
  src=$RESTIC_REPOSITORY
  export RESTIC_REPOSITORY="s3:$R2_ENDPOINT/${R2_BUCKET:-distri-respaldos}"
  restic copy --from-repo "$src" --from-password-file "$RESTIC_PASSWORD_FILE" --host distri-prod -q || exit 1
  [ "$(date +%H)" = "$PRUNE_HOUR" ] || exit 0
  restic forget --host distri-prod --prune -q "${KEEP[@]}" || exit 1
  bytes=$(restic stats --mode raw-data --json | grep -oP '"total_size":\K[0-9]+') || exit 1
  snaps=$(restic snapshots --json | grep -o '"short_id"' | wc -l) || exit 1
  set_status r2_bytes "$bytes"
  set_status r2_snapshots "$snaps"
  set_status r2_checked "$(date -Is)"
)
if [ -f "$CONF/r2.env" ]; then
  if r2_copy; then
    set_status r2_ok "$(date -Is)"; set_status r2_error ""
    echo "$(date -Is) r2 ok"
  else
    set_status r2_error "$(date -Is) copy to R2 failed, see logs/respaldo-hora.log"
    echo "$(date -Is) R2 FAILED (the local backup is fine)"
    logger -p user.err -t respaldo-hora "copy to R2 failed" 2>/dev/null || true
  fi
fi
ping ""
