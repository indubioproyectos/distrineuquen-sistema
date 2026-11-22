#!/usr/bin/env bash
# Upgrades distri_pedido in production (distri_lab), the documented way: stop
# Odoo, run the upgrade in a one-off container, start Odoo again. Production
# runs a frozen export of one commit (scripts/prod-snapshot.sh), never the
# working tree /srv/odoo-lab/addons.
#
#   scripts/actualizar-modulo.sh [módulo] [commit]   defaults: distri_pedido, main
#   scripts/actualizar-modulo.sh --rollback <snapshot>
#
# Upgrade: exports `git archive <commit> addons` to prod/snapshots/<short>,
# stops Odoo, points prod/addons-current at it, runs `-u <módulo>` in a one-off
# container on THAT snapshot, starts Odoo (docker resolves the symlink at start,
# so it comes up on the new snapshot). With the default commit it refuses when
# addons/ has uncommitted changes. If the -u fails, the symlink goes back to the
# previous snapshot before Odoo starts. Keeps the last PROD_KEEP=10 snapshots
# (always the previous one) and prints the rollback command.
#
# Rollback: stops Odoo, points addons-current at an existing snapshot, runs
# -u distri_pedido on it and starts Odoo. Migrations do not run backwards: if
# the upgrade transformed data, restore the pre-upgrade backup instead.
#
# Holds the maintenance lock (run/mantenimiento.lock), the same one the
# automatic backup and its restore test use: they never run during an
# upgrade. Take the manual backup and run the test suite on a copy first.
set -euo pipefail
LAB=/srv/odoo-lab
source "$LAB/scripts/prod-snapshot.sh"
die() { echo "ERROR: $*" >&2; exit 1; }
cd "$LAB"

if [[ "${1:-}" == --rollback ]]; then
  MODE=rollback NEW=${2:?usage: actualizar-modulo.sh --rollback <snapshot>}
  [[ -f "$PROD_SNAPS/$NEW/COMMIT" ]] || die "no snapshot $NEW in $PROD_SNAPS"
else
  MODE=upgrade MODULE=${1:-distri_pedido} COMMIT=${2:-}
  if [[ -z "$COMMIT" ]]; then
    [[ -z "$(git status --porcelain -- addons)" ]] ||
      die "uncommitted changes under addons/ (git status -- addons): commit them, or pass an explicit commit"
    COMMIT=main
  fi
  COMMIT=$(git rev-parse --verify "$COMMIT^{commit}") || die "unknown commit"
fi

mkdir -p "$LAB/run" && chmod 700 "$LAB/run"
exec 9>"$LAB/run/mantenimiento.lock"
echo "Esperando el candado de mantenimiento…"
flock -w 3600 9

# the container must already mount the symlink, or a restart would not switch code
MOUNT=$(docker inspect odoo-lab-odoo-1 \
  --format '{{range .Mounts}}{{if eq .Destination "/mnt/extra-addons"}}{{.Source}}{{end}}{{end}}')
[[ "$MOUNT" == "$PROD_CURRENT" ]] ||
  die "odoo-lab-odoo-1 mounts $MOUNT, not $PROD_CURRENT: recreate it first (docker compose up -d odoo)"

PREV=$(prod_snapshot_current)
[[ "$MODE" == upgrade ]] && NEW=$(prod_snapshot_export "$COMMIT")
docker run --rm --network none --entrypoint test -v "$PROD_SNAPS/$NEW/addons":/mnt/extra-addons:ro \
  distri-odoo:19-ocr -r /mnt/extra-addons/distri_pedido/__manifest__.py ||
  die "snapshot $NEW not readable inside the Odoo image"
echo "Código: ${PREV:-?} -> $NEW (distri_pedido $(prod_snapshot_version "$NEW"))"

on_exit() {
  if [[ -z "${OK:-}" && -n "$PREV" ]]; then
    prod_snapshot_switch "$PREV"
    echo "FALLÓ: el código vuelve a $PREV. Revisar ir_module_module (latest_version) antes de seguir." >&2
  fi
  docker compose start odoo
}
# stop first, then switch: Odoo never sees the symlink move while it runs
docker compose stop odoo
trap on_exit EXIT
prod_snapshot_switch "$NEW"
# rollback runs -u too, so views, fields and data files match the code again
[[ "$MODE" == rollback ]] && MODULE=distri_pedido
LAB_ADDONS="$PROD_SNAPS/$NEW/addons" NAME=distri-actualizar-$$ "$LAB/scripts/l10n_ar/lab.sh" \
  odoo -d distri_lab -u "$MODULE" --stop-after-init --no-http --log-level=warn
OK=1
prod_snapshot_prune "$PREV"
if [[ "$MODE" == upgrade ]]; then
  echo "Actualizado: $MODULE en el snapshot $NEW"
else
  echo "Código vuelto a $NEW (con -u $MODULE)"
fi
[[ -n "$PREV" && "$PREV" != "$NEW" ]] &&
  echo "Volver atrás: $LAB/scripts/actualizar-modulo.sh --rollback $PREV"
exit 0
