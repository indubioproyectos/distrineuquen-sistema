#!/usr/bin/env bash
# 19.0.1.129.27: turns ON «Caja v2» (ir.config_parameter
# distri_pedido.caja_v2 = 1, models/caja_v2.py) in a database: cash
# shortfalls as the employee's debt in «Deudores varios» and «Adelanto para
# la ruta» in /caja. It checks the module is at 19.0.1.129.27 or later and
# that «Deudores varios» is a receivable account other than the customers'.
# Production (distri_lab) gets the code with the flag OFF: this script
# refuses it unless --produccion is given on purpose.
#   scripts/correcciones/v129_27_activar_caja_v2.sh <base>          turn it on
#   scripts/correcciones/v129_27_activar_caja_v2.sh <base> --off    turn it off again
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
[[ -n "$DB" ]] || { echo "usage: $0 <base> [--off]" >&2; exit 2; }
[[ "$DB" =~ ^[A-Za-z0-9_]+$ ]] || { echo "bad base name $DB" >&2; exit 2; }
if [[ "$DB" == distri_lab && $PROD -ne 1 && $VALUE -eq 1 ]]; then
  echo "distri_lab is production: it keeps caja_v2 off (pass --produccion only on the owner's order)" >&2
  exit 1
fi
HERE=$(cd "$(dirname "$0")" && pwd)

PY=$(cat <<PY
from odoo.addons.distri_pedido.models.caja_v2 import PARAM_CAJA_V2, caja_v2_on, debt_account
env = env(user=2)
module = env["ir.module.module"].search([("name", "=", "distri_pedido")])
version = tuple(int(x) for x in (module.latest_version or "0").split("."))
assert module.state == "installed" and version >= (19, 0, 1, 129, 27), (
    "distri_pedido %s %s: deploy 19.0.1.129.27 first" % (module.state, module.latest_version))
if $VALUE == 1:
    account = debt_account(env, env.company)
    print("ACCOUNT", account.code, account.name, account.account_type)
env["ir.config_parameter"].sudo().set_param(PARAM_CAJA_V2, "$VALUE")
env.cr.commit()
if hasattr(env.registry, "signal_changes"):
    env.registry.signal_changes()
print("CAJA_V2", "ON" if caja_v2_on(env) else "OFF", "in", env.cr.dbname)
PY
)
FILTER=(rg -e '^(ACCOUNT|CAJA_V2)' -e 'Traceback' -e 'Error')
if [[ "$DB" == distri_practica ]]; then
  # shellcheck source=../../practica/comun.sh
  source "$HERE/../../practica/comun.sh"
  odoo_run shell -d "$DB" --no-http --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
else
  NAME=distri-caja-v2-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http \
    --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
fi
