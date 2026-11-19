#!/usr/bin/env bash
# Daily backup of the public AgilPedido storefront of DistriNeuquén
# (https://agilpedido.com/distrineuquen): page, data, import-ready
# productos.xlsx, product -> section/photo CSV and a deduplicated photo store,
# under /srv/odoo-lab/backups-agil/YYYY-MM-DD/. Runs from ale's crontab at
# 03:30 (server time is Argentina). See README.md next to this script.
#
# Host side only: reads the public page, no credentials, does not touch Odoo,
# its databases or Docker. The hourly restic backup (scripts/respaldo-hora.sh)
# carries backups-agil off-site to Cloudflare R2.
#
# On failure (page unreadable, 0 products or fewer than half of the previous
# backup's) nothing is overwritten: YYYY-MM-DD-FALLA/ with ALERTA.txt, a line
# in backups-agil/ALERTA.txt, syslog (tag respaldo-agil) and exit 2.
#
#   AGIL_FUENTE=<file>   read the page from a local file (tests)
#   AGIL_DESTINO=<dir>   backup root (tests; default /srv/odoo-lab/backups-agil)
set -euo pipefail
umask 077

HERE=$(cd "$(dirname "$0")" && pwd)
export AGIL_DESTINO=${AGIL_DESTINO:-/srv/odoo-lab/backups-agil}
mkdir -p "$AGIL_DESTINO"
LOG=$AGIL_DESTINO/respaldo-agil.log

exec >>"$LOG" 2>&1
exec 9>"$AGIL_DESTINO/.lock"
flock -n 9 || { echo "$(date -Is) another run is in progress, skipped"; exit 0; }

echo "$(date -Is) start${AGIL_FUENTE:+ (source: $AGIL_FUENTE)}"
rc=0
timeout 3600 python3 -I "$HERE/respaldo_agil.py" || rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then  # killed or crashed before it could alert
  printf '%s respaldo de AgilPedido FALLÓ: el proceso terminó con código %s (ver respaldo-agil.log)\n' \
    "$(date '+%F %T')" "$rc" >> "$AGIL_DESTINO/ALERTA.txt"
  chmod 600 "$AGIL_DESTINO/ALERTA.txt" 2>/dev/null || true
  logger -p user.err -t respaldo-agil "respaldo de AgilPedido FALLÓ: código $rc" 2>/dev/null || true
fi
echo "$(date -Is) end (exit $rc)"
exit "$rc"
