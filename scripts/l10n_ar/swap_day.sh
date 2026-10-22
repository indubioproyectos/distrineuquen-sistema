#!/usr/bin/env bash
# Migration day: the live lab (distri_lab, generic chart) becomes the
# Argentine copy (ar_base, Monotributo) with the remito-only customer view.
# DO NOT RUN without ale's approval. Procedure and rollback: SWAP.md.
#   swap_day.sh --approved-by-ale
set -euo pipefail
[ "${1:-}" = "--approved-by-ale" ] || { echo "Needs ale's approval: see SWAP.md"; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
LAB=/srv/odoo-lab
TS=$(date +%Y%m%d-%H%M)
OLD=distri_lab_generic_${TS/-/_}
PSQL="docker exec -i odoo-lab-db-1 psql -U odoo -v ON_ERROR_STOP=1 -d postgres"
step() { echo; echo "=== $(date +%T) $*"; }

step "0. the live module must still be the one the changes were made on"
if ! diff -r -q -x __pycache__ $LAB/addons-ar-base/distri_pedido $LAB/addons/distri_pedido; then
  echo "addons/distri_pedido changed after the changes were prepared (addons-ar-base)."
  echo "Re-apply the changes on top of the current module first (SWAP.md)."; exit 3
fi

step "1. stop the live Odoo (downtime starts)"
docker compose -f $LAB/compose.yaml stop odoo

step "2. backups"
docker exec odoo-lab-db-1 pg_dump -U odoo -Fc distri_lab > /srv/backups/distri_lab-$TS-swap.dump
docker run --rm -v odoo-lab_odoo-data:/var/lib/odoo odoo:19 tar czf - -C /var/lib/odoo/filestore distri_lab \
  > /srv/backups/distri_lab-filestore-$TS-swap.tgz
tar czf /srv/backups/distri_pedido-$TS-pre-swap.tgz -C $LAB/addons distri_pedido
chmod 660 /srv/backups/*-$TS-*

step "3. rebuild the Argentine copy from that dump (tests: RUN_TESTS=1)"
RUN_TESTS=${RUN_TESTS:-0} "$HERE/rebuild.sh" /srv/backups/distri_lab-$TS-swap.dump \
  /srv/backups/distri_lab-filestore-$TS-swap.tgz

step "4. deploy the module changes (same files the copy was updated with)"
rsync -a --delete --exclude __pycache__ $LAB/addons-ar-dev/distri_pedido/ $LAB/addons/distri_pedido/

step "5. swap databases and filestores"
$PSQL -c "alter database distri_lab rename to $OLD" -c "alter database distri_lab_ar rename to distri_lab"
docker run --rm -v odoo-lab_odoo-data:/var/lib/odoo odoo:19 bash -c \
  "cd /var/lib/odoo/filestore && mv distri_lab $OLD && mv distri_lab_ar distri_lab"

step "6. start the live Odoo (downtime ends)"
docker compose -f $LAB/compose.yaml start odoo
for _ in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8069/web/login)" = 200 ] && break; sleep 2
done
curl -s -o /dev/null -w "login %{http_code}\n" http://127.0.0.1:8069/web/login
curl -s -o /dev/null -w "pedido %{http_code}\n" http://127.0.0.1:8069/pedido

step "7. verify the new live DB against the pre-swap snapshot"
SNAP_TAG=swapped ADDONS=$LAB/addons "$HERE/lab.sh" odoo shell -d distri_lab --no-http --log-level=error < "$HERE/snap.py"
python3 "$HERE/compare.py" "$HERE/out/snap_before.json" "$HERE/out/snap_swapped.json"
echo "Old generic DB kept as $OLD (rollback: SWAP.md)"
