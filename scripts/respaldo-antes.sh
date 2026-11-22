#!/usr/bin/env bash
# Manual backup before a change in production (distri_lab): database dump only.
#
#   scripts/respaldo-antes.sh <motivo>     e.g. v128, nc-perez, planillas-efectivo
#
# Writes backups-db/<fecha>-antes-<motivo>-<HHMM>/distri_lab.dump (mode 600).
# No filestore: attachments are already in the hourly restic copy and in the
# daily backups-auto, and copying 850 MB per change filled the disk.
# Keeps the manual backups of the last KEEP_DAYS=7 days; older ones are
# removed, except the go-live cut (*-corte-*).
set -euo pipefail
umask 077
MOTIVO=${1:?usage: respaldo-antes.sh <motivo>}
[[ "$MOTIVO" =~ ^[a-z0-9-]+$ ]] || { echo "ERROR: motivo solo con a-z, 0-9 y guiones" >&2; exit 1; }
LAB=/srv/odoo-lab
DB=${DB:-distri_lab}
DEST=$LAB/backups-db
KEEP_DAYS=${KEEP_DAYS:-7}
NOW=$(TZ=America/Argentina/Buenos_Aires date +%F-%H%M)
DIR=$DEST/${NOW%-*}-antes-$MOTIVO-${NOW##*-}

mkdir -p "$DEST" && chmod 700 "$DEST"
TMP=$(mktemp -d "$DEST/.en-curso-XXXXXX")
trap 'rm -rf "$TMP"' EXIT
docker exec odoo-lab-db-1 pg_dump -U odoo -Fc "$DB" > "$TMP/$DB.dump"
docker exec -i odoo-lab-db-1 pg_restore --list < "$TMP/$DB.dump" > /dev/null
mv "$TMP" "$DIR" && chmod 700 "$DIR" && chmod 600 "$DIR/$DB.dump"
trap - EXIT
echo "respaldo: $DIR ($(du -h "$DIR/$DB.dump" | cut -f1))"

# rotation: manual backups older than KEEP_DAYS, never the go-live cut
find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20??-??-??-*' ! -name '*-corte-*' \
  -mtime +"$KEEP_DAYS" -print -exec rm -rf {} +
