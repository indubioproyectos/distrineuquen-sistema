#!/usr/bin/env bash
# Upgrades distri_pedido in the lab (distri_lab), the documented way: stop
# Odoo, run the upgrade in a one-off container, start Odoo again.
#
#   scripts/actualizar-modulo.sh [módulo]     default: distri_pedido
#
# Holds the maintenance lock (run/mantenimiento.lock), the same one the
# automatic backup and its restore test use: they never run during an
# upgrade. Take the manual backup and run the test suite on a copy first.
set -euo pipefail
LAB=/srv/odoo-lab
MODULE=${1:-distri_pedido}
mkdir -p "$LAB/run" && chmod 700 "$LAB/run"
exec 9>"$LAB/run/mantenimiento.lock"
echo "Esperando el candado de mantenimiento…"
flock -w 3600 9
cd "$LAB"
docker compose stop odoo
trap 'docker compose start odoo' EXIT
NAME=distri-actualizar-$$ "$LAB/scripts/l10n_ar/lab.sh" odoo -d distri_lab -u "$MODULE" \
  --stop-after-init --no-http --log-level=warn
echo "Actualizado: $MODULE"
