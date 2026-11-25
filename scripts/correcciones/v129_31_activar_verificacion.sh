#!/usr/bin/env bash
# 19.0.1.129.31: turns ON «Tablero de verificación» (ir.config_parameter
# distri_pedido.verificacion_v2 = 1, models/verificacion.py verificacion_on)
# in a database: the /verificacion screen and its Inicio tile, the customer's
# receipt credited by itself on a sure match with the report already loaded
# (always checked later by a person), round amounts and WhatsApp chat imports
# always to a person, the twin guard. The first time it is turned on it also
# writes distri_pedido.verificacion_v2_desde = now: only receipts arriving
# after that moment may credit themselves, nothing already loaded.
# It checks the module is at 19.0.1.129.31 or later.
# Production (distri_lab) gets the code with the flag OFF: this script refuses
# it unless --produccion is given on purpose.
#   scripts/correcciones/v129_31_activar_verificacion.sh <base>         turn it on
#   scripts/correcciones/v129_31_activar_verificacion.sh <base> --off   turn it off again
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
  echo "distri_lab is production: it keeps verificacion_v2 off (pass --produccion only on the owner's order)" >&2
  exit 1
fi
HERE=$(cd "$(dirname "$0")" && pwd)

PY=$(cat <<PY
from odoo import fields
from odoo.addons.distri_pedido.models.verificacion import (
    PARAM_SINCE, PARAM_VERIFICACION, verificacion_on)
env = env(user=2)
module = env["ir.module.module"].search([("name", "=", "distri_pedido")])
version = tuple(int(x) for x in (module.latest_version or "0").split("."))
assert module.state == "installed" and version >= (19, 0, 1, 129, 31), (
    "distri_pedido %s %s: deploy 19.0.1.129.31 first" % (module.state, module.latest_version))
params = env["ir.config_parameter"].sudo()
params.set_param(PARAM_VERIFICACION, "$VALUE")
if "$VALUE" == "1" and not params.get_param(PARAM_SINCE):
    params.set_param(PARAM_SINCE, fields.Datetime.to_string(fields.Datetime.now()))
env.cr.commit()
if hasattr(env.registry, "signal_changes"):
    env.registry.signal_changes()
print("VERIFICACION", "ON" if verificacion_on(env) else "OFF", "in", env.cr.dbname,
      "since", params.get_param(PARAM_SINCE) or "-")
PY
)
FILTER=(rg -e '^VERIFICACION' -e 'Traceback' -e 'Error')
if [[ "$DB" == distri_practica ]]; then
  # shellcheck source=../../practica/comun.sh
  source "$HERE/../../practica/comun.sh"
  odoo_run shell -d "$DB" --no-http --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
else
  NAME=distri-verificacion-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http \
    --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
fi
