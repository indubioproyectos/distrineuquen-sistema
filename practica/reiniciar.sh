#!/usr/bin/env bash
# Nightly reset of the practice environment (practica.distrineuquen.com).
#
#   ./reiniciar.sh                    restore distri_practica from the clean «golden»
#                                     copy, load today's practice data (sembrar.py)
#   ./reiniciar.sh --renovar-golden   first rebuild the golden copy from production:
#                                     pg_dump distri_lab + filestore (read only, under
#                                     the production maintenance lock), sanitize
#                                     (sanitizar.sql), install distri_practica,
#                                     verify (verificar.sh), dump golden; then reset
#   ./reiniciar.sh --solo-golden      like --renovar-golden without the final reset
#
# golden/: distri_practica.dump + filestore.tar + golden.txt (sanitized, before
# the daily data). Production containers are never restarted or written.
# Logs: logs/reiniciar-YYYY-MM-DD.log (30 days). Lock: work/reiniciar.lock.
set -euo pipefail
source /srv/odoo-lab/practica/comun.sh
MODE=${1:-}
case "$MODE" in ""|--renovar-golden|--solo-golden) ;; *) die "usage: $0 [--renovar-golden|--solo-golden]";; esac

umask 077
mkdir -p "$P/logs" "$WORK" "$GOLDEN"
LOGFILE=$P/logs/reiniciar-$(date +%F).log
if [[ -z "${PRACTICA_NO_LOG:-}" ]]; then
  exec > >(tee -a "$LOGFILE") 2>&1
fi
find "$P/logs" -name 'reiniciar-*.log' -mtime +30 -delete 2>/dev/null || true

exec 9>"$WORK/reiniciar.lock"
flock -w 1800 9 || die "another reset/update is running"
log "== reiniciar ${MODE:-diario} =="

PROD_DUMP=$WORK/prod.dump
PROD_FS=$WORK/prod-filestore.tar
cleanup() { rm -f "$PROD_DUMP" "$PROD_FS" "$WORK"/keep.txt "$WORK"/members.txt "$GOLDEN"/*.tmp; }
trap cleanup EXIT

renovar_golden() {
  local version commit
  version=$(prod_installed_version)
  [[ -n "$version" ]] || die "distri_pedido not installed in production?"
  commit=$(commit_for_version "$version") || die "no commit with distri_pedido $version"
  log "production distri_pedido $version -> commit $commit"
  snapshot_use "$commit"

  log "production dump (read only, maintenance lock)"
  exec 8>"$LAB/run/mantenimiento.lock"
  flock -w 3600 8 || die "production maintenance lock busy for 1h"
  docker exec odoo-lab-db-1 pg_dump -U odoo -Fc distri_lab > "$PROD_DUMP"
  docker run --rm --network none -v odoo-lab_odoo-data:/src:ro --entrypoint tar distri-odoo:19-ocr \
    cf - -C /src/filestore distri_lab > "$PROD_FS"
  flock -u 8; exec 8>&-
  log "dump $(du -h "$PROD_DUMP" | cut -f1), filestore $(du -h "$PROD_FS" | cut -f1)"

  db_up
  odoo_stop
  db_recreate
  log "restore into the practice Postgres"
  db_restore "$PROD_DUMP"
  rm -f "$PROD_DUMP"

  log "sanitize"
  pg -d "$DB" -v codigo="$(tr -dc 0-9 < "$P/.codigo")" -v base_url="https://$HOST_HDR" \
    < "$P/sanitizar.sql"

  log "filestore: only the files still referenced"
  pg -d "$DB" -Atc "select distinct 'distri_lab/' || store_fname from ir_attachment where store_fname is not null" \
    | sort > "$WORK/keep.txt"
  tar tf "$PROD_FS" | sort > "$WORK/members.txt"
  comm -12 "$WORK/keep.txt" "$WORK/members.txt" > "$WORK/keep.ok"
  log "files: $(wc -l < "$WORK/keep.txt") referenced, $(wc -l < "$WORK/keep.ok") found"
  dc run --rm --no-deps -T --user root -v "$WORK:/work:ro" --entrypoint bash odoo -c '
    set -e; mkdir -p /var/lib/odoo/filestore; cd /var/lib/odoo/filestore
    rm -rf distri_practica .nuevo; mkdir .nuevo
    tar xf /work/prod-filestore.tar -C .nuevo --files-from /work/keep.ok
    mkdir -p .nuevo/distri_lab; mv .nuevo/distri_lab distri_practica; rm -rf .nuevo
    chown -R odoo:odoo /var/lib/odoo'
  rm -f "$PROD_FS" "$WORK/keep.ok"

  log "install distri_practica"
  odoo_run -d "$DB" -i distri_practica --stop-after-init --no-http --log-level=warn
  # a module update may have added crons/params: sanitize again (idempotent)
  pg -d "$DB" -v codigo="$(tr -dc 0-9 < "$P/.codigo")" -v base_url="https://$HOST_HDR" \
    < "$P/sanitizar.sql" >/dev/null

  log "verify (no real customer data, no production secret)"
  "$P/verificar.sh" || die "verification FAILED: golden NOT replaced"

  write_golden "$commit" "$version"
}

write_golden() {  # $1 commit, $2 version
  log "write golden"
  docker exec odoo-practica-db pg_dump -U odoo -Fc "$DB" > "$GOLDEN/distri_practica.dump.tmp"
  filestore_dump "$GOLDEN/filestore.tar.tmp"
  mv "$GOLDEN/distri_practica.dump.tmp" "$GOLDEN/distri_practica.dump"
  mv "$GOLDEN/filestore.tar.tmp" "$GOLDEN/filestore.tar"
  printf 'fecha: %s\ncommit: %s\ndistri_pedido: %s\n' "$(date -Is)" "$1" "$2" > "$GOLDEN/golden.txt"
  log "golden: $(du -h "$GOLDEN/distri_practica.dump" | cut -f1) dump, $(du -h "$GOLDEN/filestore.tar" | cut -f1) filestore"
}

reset_from_golden() {
  [[ -s "$GOLDEN/distri_practica.dump" && -s "$GOLDEN/filestore.tar" ]] \
    || die "no golden copy yet: run $0 --renovar-golden"
  local commit
  commit=$(rg -o '^commit: (\S+)' -r '$1' "$GOLDEN/golden.txt")
  [[ "$(snapshot_current)" == "$commit" ]] || snapshot_use "$commit"
  db_up
  odoo_stop
  db_recreate
  log "restore golden ($(rg '^fecha' "$GOLDEN/golden.txt"))"
  db_restore "$GOLDEN/distri_practica.dump"
  filestore_restore "$GOLDEN/filestore.tar"
  log "practice data of today"
  odoo_run shell -d "$DB" --no-http --log-level=warn < "$P/sembrar.py" | rg '^(SEMBRADO|COMMITTED|WARN)' \
    || die "sembrar.py failed"
  # recreate the container when the addons snapshot changed
  dc up -d --force-recreate odoo >/dev/null
  odoo_start
  health_check
}

if [[ "$MODE" == --renovar-golden || "$MODE" == --solo-golden ]]; then
  renovar_golden
fi
if [[ "$MODE" != --solo-golden ]]; then
  reset_from_golden
else
  odoo_start
fi
log "== listo =="
