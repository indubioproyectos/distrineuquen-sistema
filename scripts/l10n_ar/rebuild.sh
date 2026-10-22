#!/usr/bin/env bash
# Rebuild the Argentine copy (distri_lab_ar) from a dump of the live lab.
# Only reads the live DB (pg_dump); every write goes to the copy.
#   rebuild.sh /srv/backups/distri_lab-YYYYMMDD-HHMM-....dump
# Steps: restore -> filestore copy -> snapshot -> install l10n_ar -> es_ES
# translations -> chart ar_base (Monotributo) + journals + replay of the lab
# test documents -> module update (dev addons) -> verification.
set -euo pipefail
DUMP=${1:?dump file}
FILESTORE_TGZ=${2:-}
COPY=distri_lab_ar
HERE=$(cd "$(dirname "$0")" && pwd)
LAB="$HERE/lab.sh"
PSQL="docker exec -i odoo-lab-db-1 psql -U odoo -v ON_ERROR_STOP=1"
step() { echo; echo "=== $(date +%T) $*"; }

step "restore $DUMP into $COPY"
$PSQL -d postgres -c "drop database if exists $COPY"
$PSQL -d postgres -c "create database $COPY owner odoo"
docker exec -i odoo-lab-db-1 pg_restore -U odoo -d $COPY --no-owner --role=odoo < "$DUMP"

step "filestore"
if [ -n "$FILESTORE_TGZ" ]; then
  "$LAB" bash -c "rm -rf /var/lib/odoo/filestore/$COPY && mkdir -p /tmp/fs && tar xzf - -C /tmp/fs && mv /tmp/fs/distri_lab /var/lib/odoo/filestore/$COPY" < "$FILESTORE_TGZ"
else
  "$LAB" bash -c "rm -rf /var/lib/odoo/filestore/$COPY && cp -a /var/lib/odoo/filestore/distri_lab /var/lib/odoo/filestore/$COPY"
fi

step "snapshot before (the copy is identical to live here)"
SNAP_TAG=before "$LAB" odoo shell -d $COPY --no-http --log-level=warn < "$HERE/snap.py"

step "install l10n_ar"
"$LAB" odoo -d $COPY -i l10n_ar --stop-after-init --no-http --log-level=warn

step "es_ES translations from es_419"
"$LAB" odoo shell -d $COPY --no-http --log-level=warn < "$HERE/i18n.py"

step "chart ar_base, journals, lab test documents"
DO_COMMIT=1 "$LAB" odoo shell -d $COPY --no-http --log-level=warn < "$HERE/convert.py"

step "update distri_pedido with the pending module changes"
"$LAB" odoo -d $COPY -u distri_pedido --stop-after-init --no-http --log-level=warn

step "verify"
SNAP_TAG=after "$LAB" odoo shell -d $COPY --no-http --log-level=warn < "$HERE/snap.py"
python3 "$HERE/compare.py" "$HERE/out/snap_before.json" "$HERE/out/snap_after.json"

if [ "${RUN_TESTS:-1}" = 1 ]; then
  step "module tests on the copy (own port, own db filter)"
  "$LAB" odoo -d $COPY --test-tags /distri_pedido --stop-after-init --http-port 8169 \
    --db-filter "^$COPY\$" --workers 0 --max-cron-threads 0 --log-level=test 2>&1 \
    | tee "$HERE/out/tests.log" | grep -E "tests.result|FAIL|ERROR" || true
  grep -q " 0 failed, 0 error(s)" "$HERE/out/tests.log"
fi
step "done"
