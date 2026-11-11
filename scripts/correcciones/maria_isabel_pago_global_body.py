# X - MARIA ISABEL ACCHURA MALLON - ALMACEN SOFIA (CENTENARIO) (ODOO-180): her
# 15 invoices of 04-08/2025 ($5.421.920) are settled in Online by one global
# payment, BNK1/2025/00881 of 02/09/2025 for exactly that amount. Her 14 MP
# transfers of those months ($5.253.670, CUIT 27-37383605-8 «MERCADOSOFIA»)
# stayed pending because the board links one Online payment per transfer.
# Discarded as already in Online (ale, 29/10/2026). No balance changes.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
partner = uenv['res.partner'].sudo().browse(551)
assert partner.ref == 'ODOO-180'
row = uenv['distri.cuenta.movimiento'].sudo().search([('partner_id', '=', partner.id), ('comprobante', '=', 'BNK1/2025/00881')])
assert len(row) == 1 and abs(row.haber - 5421920) < 0.01, row.mapped('haber')
cards = uenv['distri.mp.collection'].sudo().search([('kind', '=', 'income'), ('state', '=', 'draft'),
                                                   ('partner_id', '=', partner.id), ('date', '<', '2025-09-03')])
total = sum(cards.mapped('amount'))
assert len(cards) == 14 and abs(total - 5253670) < 0.01 and set(cards.mapped('payer_cuit')) == {'27373836058'}, (len(cards), total)
before = partner._distri_account_balance()
note = 'Ya en Online: BNK1/2025/00881 (02/09/2025), pago global de $ 5.421.920 de las facturas de 04-08/2025; revisado 29/10/2026'
for card in cards:
    card.write({'state': 'ignored', 'ignore_reason': 'other', 'ignore_note': note})
    card.message_post(body='Descartado: %s.' % note)
uenv['distri.balance.review'].sudo()._distri_refresh(partner)
env.flush_all()
after = partner._distri_account_balance()
print('RESULT descartadas=%d importe=%.2f saldo %.2f -> %.2f' % (len(cards), total, before, after))
if DO_COMMIT and abs(after - before) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
