#!/usr/bin/env bash
# 19.0.1.129.36 (owner's decision B, 25/11/2026): puts Denis, Gonzalo and Ale
# (the admin user, «Ale · Sistemas») in «Facturación directa de servicios»
# (distri_pedido.grupo_facturacion_directa, models/factura_directa.py), by
# login. While distri_pedido.nueva_venta is on, only that group posts a
# customer invoice that does not come from a sale order, and only with
# services. Run it BEFORE turning «Nueva venta» on (v129_30_activar_nueva_
# venta.sh), or those three cannot post their direct service invoices.
# Archived users are added too (the group waits for them); nobody is removed.
# It checks the module is at 19.0.1.129.36 or later. Dry run unless --apply.
#   scripts/correcciones/v129_36_facturacion_directa.sh <base>                    list what it would do
#   scripts/correcciones/v129_36_facturacion_directa.sh <base> --apply            add them
#   scripts/correcciones/v129_36_facturacion_directa.sh <base> --login x --apply  only login x (repeatable)
# Production (distri_lab) needs --produccion with --apply, on the owner's order.
# distri_practica runs in the practice environment (practica/comun.sh
# odoo_run); any other base through scripts/l10n_ar/lab.sh.
set -euo pipefail
DB="" APPLY=0 PROD=0 LOGINS=() NEXT_LOGIN=0
for a in "$@"; do
  if [[ $NEXT_LOGIN -eq 1 ]]; then LOGINS+=("$a"); NEXT_LOGIN=0; continue; fi
  case $a in
    --apply) APPLY=1 ;;
    --produccion) PROD=1 ;;
    --login) NEXT_LOGIN=1 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) DB=$a ;;
  esac
done
[[ -n "$DB" ]] || { echo "usage: $0 <base> [--apply] [--login <login>]... [--produccion]" >&2; exit 2; }
[[ "$DB" =~ ^[A-Za-z0-9_]+$ ]] || { echo "bad base name $DB" >&2; exit 2; }
for l in "${LOGINS[@]}"; do
  [[ "$l" =~ ^[A-Za-z0-9_.@-]+$ ]] || { echo "bad login $l" >&2; exit 2; }
done
if [[ "$DB" == distri_lab && $PROD -ne 1 && $APPLY -eq 1 ]]; then
  echo "distri_lab is production: pass --produccion only on the owner's order" >&2
  exit 1
fi
HERE=$(cd "$(dirname "$0")" && pwd)
PYLOGINS=""
for l in "${LOGINS[@]}"; do PYLOGINS+="\"$l\", "; done

PY=$(cat <<PY
from odoo.addons.distri_pedido.models.factura_directa import LOGINS_DIRECTA
env = env(user=2)
module = env["ir.module.module"].search([("name", "=", "distri_pedido")])
version = tuple(int(x) for x in (module.latest_version or "0").split("."))
assert module.state == "installed" and version >= (19, 0, 1, 129, 36), (
    "distri_pedido %s %s: deploy 19.0.1.129.36 first" % (module.state, module.latest_version))
logins = tuple([$PYLOGINS]) or LOGINS_DIRECTA
for line in env["res.users"]._distri_asignar_facturacion_directa(logins, apply=bool($APPLY)):
    print("DIRECTA", line)
group = env.ref("distri_pedido.grupo_facturacion_directa")
members = group.with_context(active_test=False).user_ids
print("DIRECTA", "members:", ", ".join("%s%s" % (u.login, "" if u.active else " (archived)")
                                       for u in members) or "none")
if $APPLY:
    env.cr.commit()
    print("DIRECTA", "committed in", env.cr.dbname)
else:
    env.cr.rollback()
    print("DIRECTA", "dry run, nothing written in", env.cr.dbname)
PY
)
FILTER=(rg -e '^DIRECTA' -e 'Traceback' -e 'Error')
if [[ "$DB" == distri_practica ]]; then
  # shellcheck source=../../practica/comun.sh
  source "$HERE/../../practica/comun.sh"
  odoo_run shell -d "$DB" --no-http --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
else
  NAME=distri-facturacion-directa-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http \
    --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
fi
