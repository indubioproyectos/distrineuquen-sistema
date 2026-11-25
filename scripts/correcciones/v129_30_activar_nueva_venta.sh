#!/usr/bin/env bash
# 19.0.1.129.30: turns ON «Nueva venta» (ir.config_parameter
# distri_pedido.nueva_venta = 1, models/nueva_venta.py nueva_venta_on) in a
# database: the /nueva-venta screen and its tile, first in Inicio, for
# sellers, administración and gerencia. «Mostrador» and «Ya entregado» are
# for administración and gerencia only. Every other sale path stays as is.
# It checks the module is at 19.0.1.129.30 or later.
# 19.0.1.129.39: turning it ON is refused (nothing is written) while
# «Facturación directa de servicios» has no active user: run
# v129_36_facturacion_directa.sh <base> --apply first. --off is never refused.
# Production (distri_lab) gets the code with the flag OFF: this script refuses
# it unless --produccion is given on purpose.
#   scripts/correcciones/v129_30_activar_nueva_venta.sh <base>         turn it on
#   scripts/correcciones/v129_30_activar_nueva_venta.sh <base> --off   turn it off again
# distri_practica runs in the practice environment (practica/comun.sh
# odoo_run); any other base through scripts/l10n_ar/lab.sh. The running Odoo
# picks the change up on its next request (the registry is signalled).
set -euo pipefail
DB="" VALUE=1 PROD=0
for a in "$@"; do
  case $a in
    --off) VALUE=0 ;;
    --produccion) PROD=1 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) DB=$a ;;
  esac
done
[[ -n "$DB" ]] || { echo "usage: $0 <base> [--off] [--produccion]" >&2; exit 2; }
[[ "$DB" =~ ^[A-Za-z0-9_]+$ ]] || { echo "bad base name $DB" >&2; exit 2; }
if [[ "$DB" == distri_lab && $PROD -ne 1 && $VALUE -eq 1 ]]; then
  echo "distri_lab is production: it keeps nueva_venta off (pass --produccion only on the owner's order)" >&2
  exit 1
fi
HERE=$(cd "$(dirname "$0")" && pwd)

PY=$(cat <<PY
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA, nueva_venta_on
env = env(user=2)
module = env["ir.module.module"].search([("name", "=", "distri_pedido")])
version = tuple(int(x) for x in (module.latest_version or "0").split("."))
assert module.state == "installed" and version >= (19, 0, 1, 129, 30), (
    "distri_pedido %s %s: deploy 19.0.1.129.30 first" % (module.state, module.latest_version))
if "$VALUE" == "1" and version >= (19, 0, 1, 129, 39):
    from odoo.addons.distri_pedido.models.factura_directa import activation_refusal
    refusal = activation_refusal(env)
    if refusal:
        print("NUEVA_VENTA", "REFUSED:", refusal)
        print("NUEVA_VENTA", "OFF" if not nueva_venta_on(env) else "ON", "in", env.cr.dbname,
              "(unchanged)")
        env.cr.rollback()
        raise SystemExit(3)
env["ir.config_parameter"].sudo().set_param(PARAM_NUEVA_VENTA, "$VALUE")
env.cr.commit()
if hasattr(env.registry, "signal_changes"):
    env.registry.signal_changes()
print("NUEVA_VENTA", "ON" if nueva_venta_on(env) else "OFF", "in", env.cr.dbname)
PY
)
FILTER=(rg -e '^NUEVA_VENTA' -e 'Traceback' -e 'Error')
if [[ "$DB" == distri_practica ]]; then
  # shellcheck source=../../practica/comun.sh
  source "$HERE/../../practica/comun.sh"
  odoo_run shell -d "$DB" --no-http --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
else
  NAME=distri-nueva-venta-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http \
    --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
fi
