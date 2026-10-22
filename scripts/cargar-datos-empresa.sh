#!/usr/bin/env bash
# Loads the company's real data into the Odoo system parameters from a private
# file (never in the code, never in git). Prints no value. Restarts Odoo.
#   /srv/odoo-lab/.empresa.env  (permissions 600), one VAR=value per line:
#     DISTRI_CUENTA_PROPIA     -> distri_pedido.cuenta_propia   (CUIT, titular, nombre, CVU/CBU, alias; separados por coma)
#     DISTRI_TELEFONO_EMPRESA  -> distri_pedido.telefono_empresa (teléfono público de la ficha, sin 54/9)
#     DISTRI_WHATSAPP_EMPRESA  -> distri_pedido.whatsapp_empresa (número de los enlaces wa.me, con 549)
# Usage: cargar-datos-empresa.sh [DB]   (default distri_lab; a copy name to try it on a copy)
set -euo pipefail
DB=${1:-distri_lab}
F=/srv/odoo-lab/.empresa.env
[ -f "$F" ] || { echo "Falta $F"; exit 1; }
[ "$(stat -c %a "$F")" = "600" ] || { echo "$F tiene que tener permisos 600"; exit 1; }
cd /srv/odoo-lab
python3 - "$F" <<'PY' | docker compose exec -T odoo odoo shell -c /etc/odoo/odoo.conf -d "$DB" --no-http 2>/dev/null | tail -1
import sys
claves = {"DISTRI_CUENTA_PROPIA": "cuenta_propia", "DISTRI_TELEFONO_EMPRESA": "telefono_empresa",
          "DISTRI_WHATSAPP_EMPRESA": "whatsapp_empresa"}
print("P = env['ir.config_parameter'].sudo()")
cargadas = []
for linea in open(sys.argv[1], encoding="utf-8"):
    k, _, v = linea.strip().partition("=")
    if k in claves and v.strip():
        print("P.set_param(%r, %r)" % ("distri_pedido." + claves[k], v.strip()))
        cargadas.append(k)
print("env.cr.commit()")
print("print(%r)" % ("datos de la empresa cargados: %s" % (", ".join(cargadas) or "ninguno")))
PY
if [ "$DB" = "distri_lab" ]; then docker compose restart odoo >/dev/null; fi
