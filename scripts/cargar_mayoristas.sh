#!/usr/bin/env bash
# Loads the known distribuidora CUITs as «Pagadores mayoristas» (payers out of
# the system, models/mayoristas.py) and sets aside their pending payments.
# The real data stays in the private folder; nothing goes into the module.
# Usage: cargar_mayoristas.sh [DB] [JSON]
#   DB   default distri_lab (pass a copy name to try it on a copy)
#   JSON default /srv/docs/distrineuquen-modulo/insumos/cruce/cuit_distribuidoras.json
#        ({"CUIT": "name", ...})
# Prints only counts. Idempotent: an existing rule is kept (and reactivated).
set -euo pipefail
DB=${1:-distri_lab}
JSON=${2:-/srv/docs/distrineuquen-modulo/insumos/cruce/cuit_distribuidoras.json}
cd /srv/odoo-lab
python3 - "$JSON" <<'PY' | docker compose exec -T odoo odoo shell -c /etc/odoo/odoo.conf -d "$DB" --no-http 2>/dev/null | tail -1
import json, sys
data = json.load(open(sys.argv[1]))
print("R = env['distri.mp.wholesale.rule'].sudo().with_context(active_test=False)")
print("rules = R.browse()")
for cuit, name in sorted(data.items()):
    print("r = R.search([('cuit', '=', %r)], limit=1)" % cuit)
    print("r = r.write({'active': True}) and r if r else R.create({'cuit': %r, 'label': %r, "
          "'note': 'Lista de distribuidoras del cruce histórico (cuit_distribuidoras.json, 29/09/2026)'})"
          % (cuit, "Distribuidora · %s" % name))
    print("rules |= r")
print("rules._compute_conflict()")
print("moved = rules._distri_apply()")
print("env.cr.commit()")
print("print('reglas: %s · en conflicto: %s · pagos apartados ahora: %s' % (len(rules), "
      "len(rules.filtered('conflict')), len(moved)))")
PY
