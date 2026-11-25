# PAINEN BELEN (377) cancelled the purchase S00212 ($127.000). Its invoice
# 0001/2026/08084 was already cancelled on 24/11/2026, but the order stayed
# packed and pending at «Entrega en depósito», and its remito R-00079 (done at
# packing) kept the goods out of stock. Ale (24/11/2026): return R-00079 to
# stock and cancel the order. Rolls back unless DO_COMMIT=1.
import os
from markupsafe import Markup

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
env = env(user=2)
order = env['sale.order'].sudo().browse(245)
assert order.name == 'S00212' and order.partner_id.id == 377 and order.state == 'sale', order.read(['name', 'state'])
assert all(m.state == 'cancel' for m in order.invoice_ids), order.invoice_ids.mapped('state')
# 19-20/11 Italo tried three times: not delivered, unloaded, routes settled (history only)
assert all(s.state == 'not_delivered' and s.unloaded and s.route_id.state == 'settled' for s in order.distri_stop_ids), order.distri_stop_ids
pick = env['stock.picking'].sudo().search([('origin', '=', 'S00212'), ('name', '=', 'R-00079')])
assert len(pick) == 1 and pick.state == 'done'
before = {m.product_id.id: m.quantity for m in pick.move_ids}

wiz = env['stock.return.picking'].sudo().with_context(active_id=pick.id, active_ids=pick.ids,
                                                       active_model='stock.picking').create({'picking_id': pick.id})
for line in wiz.product_return_moves:
    line.quantity = line.move_id.quantity
ret = wiz._create_return()
for move in ret.move_ids:
    move.quantity = move.product_uom_qty
    move.picked = True
res = ret.button_validate()
assert ret.state == 'done', (ret.state, res)
returned = {m.product_id.id: m.quantity for m in ret.move_ids}
assert returned == before, (before, returned)

order._action_cancel()
assert order.state == 'cancel'
body = Markup('<b>Compra cancelada por el cliente</b> (24/11/2026): ') + (
    'la factura 0001/2026/08084 ya estaba cancelada; la mercadería del remito R-00079 volvió al stock '
    'con %s y el pedido se canceló (sale de Entrega en depósito). Pidió Ale.' % ret.name)
order.message_post(body=body)
order.partner_id.message_post(body=body)
env.flush_all()
print('RESULT %s %s | devolución %s %s, %d productos, %.0f unidades' % (
    order.name, order.state, ret.name, ret.state, len(returned), sum(returned.values())))
if DO_COMMIT:
    env.cr.commit(); print('COMMITTED')
else:
    env.cr.rollback(); print('NOT COMMITTED')
