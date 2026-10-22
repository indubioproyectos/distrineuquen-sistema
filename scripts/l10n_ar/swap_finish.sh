#!/usr/bin/env bash
# Finishes a swap_day.sh run that stopped at step 5 (26/09/2026: the old DB
# name had a "-" that Postgres rejected). Steps 1-4 already ran: Odoo is
# stopped, distri_lab_ar is rebuilt and verified, addons/ has the new module.
#   swap_finish.sh --approved-by-ale
set -euo pipefail
[ "${1:-}" = "--approved-by-ale" ] || { echo "Needs ale's approval: see SWAP.md"; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
LAB=/srv/odoo-lab
OLD=distri_lab_generic_20260926_2246
PSQL="docker exec -i odoo-lab-db-1 psql -U odoo -v ON_ERROR_STOP=1 -d postgres"
step() { echo; echo "=== $(date +%T) $*"; }

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
