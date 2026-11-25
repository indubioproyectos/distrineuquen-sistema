#!/usr/bin/env bash
# 19.0.1.129.28: turns ON «Reparto v2» (ir.config_parameter
# distri_pedido.reparto_v2 = 1, models/reparto_v2.py and
# static/src/reparto/app5.js) in a database: the one-screen visit, «Mis
# comprobantes a reclamar», the open-route reminder, «Ya estaba pagado» with
# debt accepted «a revisar», a partial payment charged to the driver, a late
# receipt closing the charge and the driver's own visit order. It checks the
# module is at 19.0.1.129.28 or later.
# Decision C (customers with «Cuenta corriente» or the hidden list exempt from
# the partial-payment charge) is PENDING OWNER CONFIRMATION: it is on with the
# flag unless distri_pedido.reparto_v2_exento_cc is 0 (--sin-exencion sets it).
# Production (distri_lab) gets the code with the flag OFF: this script refuses
# it unless --produccion is given on purpose.
#   scripts/correcciones/v129_28_activar_reparto_v2.sh <base>                 turn it on
#   scripts/correcciones/v129_28_activar_reparto_v2.sh <base> --sin-exencion  on, decision C off
#   scripts/correcciones/v129_28_activar_reparto_v2.sh <base> --off           turn it off again
# distri_practica runs in the practice environment (practica/comun.sh
# odoo_run); any other base through scripts/l10n_ar/lab.sh. The running Odoo
# picks the change up on its next request (the registry is signalled); the
# phones on their next «Actualizar la ruta» or download.
set -euo pipefail
DB="" VALUE=1 PROD=0 EXEMPT=1
for a in "$@"; do
  case $a in
    --off) VALUE=0 ;;
    --sin-exencion) EXEMPT=0 ;;
    --produccion) PROD=1 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) DB=$a ;;
  esac
done
[[ -n "$DB" ]] || { echo "usage: $0 <base> [--off] [--sin-exencion]" >&2; exit 2; }
[[ "$DB" =~ ^[A-Za-z0-9_]+$ ]] || { echo "bad base name $DB" >&2; exit 2; }
if [[ "$DB" == distri_lab && $PROD -ne 1 && $VALUE -eq 1 ]]; then
  echo "distri_lab is production: it keeps reparto_v2 off (pass --produccion only on the owner's order)" >&2
  exit 1
fi
HERE=$(cd "$(dirname "$0")" && pwd)

PY=$(cat <<PY
from odoo.addons.distri_pedido.models.reparto_v2 import (
    PARAM_EXEMPT, PARAM_REPARTO_V2, exempt_on, reparto_v2_on)
env = env(user=2)
module = env["ir.module.module"].search([("name", "=", "distri_pedido")])
version = tuple(int(x) for x in (module.latest_version or "0").split("."))
assert module.state == "installed" and version >= (19, 0, 1, 129, 28), (
    "distri_pedido %s %s: deploy 19.0.1.129.28 first" % (module.state, module.latest_version))
params = env["ir.config_parameter"].sudo()
params.set_param(PARAM_REPARTO_V2, "$VALUE")
if $VALUE == 1:
    params.set_param(PARAM_EXEMPT, "$EXEMPT")
env.cr.commit()
if hasattr(env.registry, "signal_changes"):
    env.registry.signal_changes()
print("REPARTO_V2", "ON" if reparto_v2_on(env) else "OFF", "in", env.cr.dbname)
print("EXENCION_CC", "ON" if exempt_on(env) else "OFF", "(decision C, pending owner confirmation)")
PY
)
FILTER=(rg -e '^(REPARTO_V2|EXENCION_CC)' -e 'Traceback' -e 'Error')
if [[ "$DB" == distri_practica ]]; then
  # shellcheck source=../../practica/comun.sh
  source "$HERE/../../practica/comun.sh"
  odoo_run shell -d "$DB" --no-http --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
else
  NAME=distri-reparto-v2-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http \
    --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
fi
