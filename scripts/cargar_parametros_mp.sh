#!/usr/bin/env bash
# Copies MP_CLIENT_SECRET from .mercadopago_test.env into the Odoo system
# parameter distri_pedido.mp_client_secret and restarts Odoo. Prints no value.
# The other MP parameters are already loaded and are left untouched.
set -euo pipefail
cd /srv/odoo-lab
python3 - .mercadopago_test.env <<'PY' | docker compose exec -T odoo odoo shell -c /etc/odoo/odoo.conf -d distri_lab --no-http 2>/dev/null | tail -1
import sys
claves = {"MP_CLIENT_SECRET": "mp_client_secret"}
print("P = env['ir.config_parameter'].sudo()")
cargadas = []
for linea in open(sys.argv[1]):
    k, _, v = linea.strip().partition("=")
    if k in claves and v.strip():
        print("P.set_param(%r, %r)" % ("distri_pedido." + claves[k], v.strip()))
        cargadas.append(k)
print("env.cr.commit()")
print("print(%r)" % ("parametros de Mercado Pago cargados: %s" % (", ".join(cargadas) or "ninguno (vacio)")))
PY
docker compose restart odoo
