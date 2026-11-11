# PUNTO DE VENTA - REYES MARRONI JUAN PABLO (ODOO-145): the $317.440 Mercado
# Pago transfer of 13/08/2026 from his account («Punto de Venta», CUIT
# 20-36800755-3) pays invoice 0001/2026/04852 of the same day, exact amount,
# with no receipt in the group. Approved without receipt on ale's word
# (30/10/2026), the board's own «Aprobar sin comprobante».
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
partner = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-145')])
assert len(partner) == 1 and 'MARRONI' in partner.name
card = uenv['distri.mp.collection'].sudo().browse(25794)
assert card.state == 'draft' and card.partner_id == partner and abs(card.amount - 317440) < 0.01
assert card.payer_cuit == '20368007553' and card.receipt_candidate and not card.receipt_id
before = partner._distri_account_balance()
uenv['distri.mp.vouch'].sudo().create({'collection_id': card.id, 'note':
    'Lo confirmó ale el 30/10/2026: transferencia desde su cuenta «Punto de Venta» por el importe exacto de la '
    'factura 0001/2026/04852 del mismo día'}).action_apply()
env.flush_all()
after = partner._distri_account_balance()
print('RESULT', card.state, card.payment_id.name, 'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and card.state == 'confirmed' and abs(before - after - 317440) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
