# X - CARRIZO CELIA ELIZABETH (CENTENARIO) (ODOO-80): the Online payment of
# 14/08/2026 was typed $289.000; the Mercado Pago transfer (card 25752, with
# receipt in the group) is $289.800. ale fixes Online by hand (30/10/2026);
# the lab's copy of the history row is corrected the same way, like Muñoz's
# PBNK1/2026/03007. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
partner = env['res.partner'].sudo().search([('ref', '=', 'ODOO-80')])
assert len(partner) == 1 and 'CARRIZO CELIA' in partner.name
Mov = env['distri.cuenta.movimiento'].sudo()
row = Mov.search([('partner_id', 'child_of', partner.id), ('tipo', '=', 'pago'), ('fecha', '=', '2026-08-14'),
                  ('haber', '>=', 288999), ('haber', '<=', 289001)])
assert len(row) == 1, row.mapped('comprobante')
card = env['distri.mp.collection'].sudo().browse(25752)
assert abs(card.amount - 289800) < 0.01
mirror = env['distri.online.payment'].sudo().search([('name', '=', row.comprobante)])
before = partner._distri_account_balance()
row.write({'haber': 289800.0, 'concepto': '%s · corregido en el lab el 30/10/2026: en Online figuraba $289.000; '
                                          'Mercado Pago $289.800' % (row.concepto or '')})
if len(mirror) == 1 and abs(mirror.amount - 289000) < 0.01:
    mirror.write({'amount': 289800.0})
partner.message_post(body='Pago %s del 14/08/2026 corregido en la historia: $289.000 → $289.800 (Mercado Pago).' % row.comprobante)
env['distri.balance.review'].sudo()._distri_refresh(partner)
env.flush_all()
after = partner._distri_account_balance()
print('RESULT', row.comprobante, 'espejo', len(mirror), 'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and abs(before - after - 800) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
