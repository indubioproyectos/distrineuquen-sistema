# Every imported history row as the customer sees it: no invoice number.
import collections, re
from odoo.addons.distri_pedido.controllers.main import TomaDePedidos
from odoo.addons.distri_pedido.models.remito import es_numero_de_factura
c = TomaDePedidos()
seen = collections.Counter()
leaks = 0
for m in env['distri.cuenta.movimiento'].sudo().search([]):
    tipo, numero = c._fila_importada(m)
    seen[(m.tipo, tipo, bool(numero))] += 1
    if es_numero_de_factura(numero) or re.search(r'\d{4}/\d{4}/\d{5}', numero or '') and numero.startswith('0'):
        leaks += 1
for k, v in sorted(seen.items()):
    print("  history tipo=%-8s -> shown as %-16s with number=%s : %d" % (k[0], k[1], k[2], v))
print("HISTORY ROWS", sum(seen.values()), "INVOICE NUMBERS SHOWN", leaks)
env.cr.rollback()
