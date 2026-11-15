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
restic forget --host distri-prod --prune --keep-hourly 24 --keep-daily 7 --keep-weekly 4 --keep-monthly 6
rm -rf "$STAGE"
echo "$(date -Is) ok"; ping ""
