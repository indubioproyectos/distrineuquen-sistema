#!/usr/bin/env bash
# End-to-end check on a THROWAWAY clone of the copy (distri_lab_ar_flow):
# auto-invoice after packing, reparto settlement to Efectivo + credit note,
# and every customer page grepped for invoice numbers. The clone is dropped
# at the end; distri_lab_ar and the live lab are not touched.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
LAB="$HERE/lab.sh"
CLONE=distri_lab_ar_flow
PSQL="docker exec -i odoo-lab-db-1 psql -U odoo -v ON_ERROR_STOP=1 -d postgres"
cleanup() {
  docker stop distri-ar-web >/dev/null 2>&1 || true
  $PSQL -c "drop database if exists $CLONE" >/dev/null
  docker run --rm -v odoo-lab_odoo-data:/var/lib/odoo odoo:19 rm -rf /var/lib/odoo/filestore/$CLONE
}
trap cleanup EXIT
$PSQL -c "drop database if exists $CLONE" -c "create database $CLONE template distri_lab_ar owner odoo" >/dev/null
"$LAB" bash -c "rm -rf /var/lib/odoo/filestore/$CLONE; cp -a /var/lib/odoo/filestore/distri_lab_ar /var/lib/odoo/filestore/$CLONE"
DO_COMMIT=1 "$LAB" odoo shell -d $CLONE --no-http --log-level=error < "$HERE/flow.py"
"$LAB" odoo shell -d $CLONE --no-http --log-level=error < "$HERE/pages_prepare.py"
NAME=distri-ar-web "$LAB" odoo -d $CLONE --db-filter "^$CLONE\$" --http-port 8169 --workers 0 \
  --max-cron-threads 0 --log-level=warn > "$HERE/out/web.log" 2>&1 &
for _ in $(seq 1 60); do
  [ "$(docker exec distri-ar-web curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8169/web/login 2>/dev/null)" = 200 ] && break
  sleep 2
done
docker exec distri-ar-web python3 /scripts/check_pages.py
"$LAB" odoo shell -d distri_lab_ar --no-http --log-level=error < "$HERE/history_labels.py"
