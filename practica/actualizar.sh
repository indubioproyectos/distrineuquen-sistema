#!/usr/bin/env bash
# Bring the practice environment to the code production now runs.
#   ./actualizar.sh <commit>      e.g. ./actualizar.sh 28253f7
#   ./actualizar.sh               the commit matching distri_pedido's installed version
# Call it right after production is upgraded (after scripts/actualizar-modulo.sh).
# Steps: export `git archive <commit> addons` into snapshots/<commit> (never
# reads the working tree of /srv/odoo-lab/addons), restore the clean golden
# copy, `-u distri_pedido,distri_tema,distri_practica` on it, sanitize again
# (a new version may add crons/params), verify, save it as the new golden,
# then load today's practice data. On any failure the old golden stays.
set -euo pipefail
source /srv/odoo-lab/practica/comun.sh
umask 077
mkdir -p "$P/logs" "$WORK"
exec > >(tee -a "$P/logs/actualizar-$(date +%F).log") 2>&1
exec 9>"$WORK/reiniciar.lock"
flock -w 1800 9 || die "another reset/update is running"

if [[ $# -ge 1 ]]; then
  COMMIT=$(git -C "$LAB" rev-parse --verify "$1^{commit}") || die "unknown commit $1"
else
  VERSION=$(prod_installed_version)
  COMMIT=$(commit_for_version "$VERSION") || die "no commit with distri_pedido $VERSION"
fi
[[ -s "$GOLDEN/distri_practica.dump" ]] || die "no golden copy yet: run reiniciar.sh --renovar-golden"
VERSION_NEW=$(git -C "$LAB" show "$COMMIT:addons/distri_pedido/__manifest__.py" | rg -o '"version": "([^"]+)"' -r '$1')
log "== actualizar a $COMMIT (distri_pedido $VERSION_NEW) =="
PREV=$(snapshot_current)
trap '[[ -n "${OK:-}" ]] || { log "FAILED: back to snapshot $PREV"; ln -sfn "snapshots/$PREV/addons" "$P/addons-prod"; }' EXIT

snapshot_use "$COMMIT"
db_up
odoo_stop
db_recreate
db_restore "$GOLDEN/distri_practica.dump"
filestore_restore "$GOLDEN/filestore.tar"
log "update modules"
odoo_run -d "$DB" -u distri_pedido,distri_tema,distri_practica --stop-after-init --no-http --log-level=warn
pg -d "$DB" -v codigo="$(tr -dc 'a-zA-Z0-9' < "$P/.codigo")" -v base_url="https://$HOST_HDR" < "$P/sanitizar.sql" >/dev/null
"$P/verificar.sh" || die "verification FAILED: golden NOT replaced"

log "write golden"
docker exec odoo-practica-db pg_dump -U odoo -Fc "$DB" > "$GOLDEN/distri_practica.dump.tmp"
filestore_dump "$GOLDEN/filestore.tar.tmp"
mv "$GOLDEN/distri_practica.dump.tmp" "$GOLDEN/distri_practica.dump"
mv "$GOLDEN/filestore.tar.tmp" "$GOLDEN/filestore.tar"
printf 'fecha: %s\ncommit: %s\ndistri_pedido: %s\n' "$(date -Is)" "$COMMIT" "$VERSION_NEW" > "$GOLDEN/golden.txt"
OK=1
flock -u 9
PRACTICA_NO_LOG=1 "$P/reiniciar.sh"
log "== listo =="
