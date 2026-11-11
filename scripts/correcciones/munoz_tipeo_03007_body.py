# MUÑOZ MARCELO - MERCADITO LA PREFERIDA (ODOO-623): the Online payment
# PBNK1/2026/03007 of 14/08/2026 was typed $1.982.350; the Mercado Pago
# transfer (operation 173895300646, card 25756, WhatsApp receipt 12947) is
# $182.350, which with the credit note 05024 ($27.900) pays invoice 04919
# ($210.250). ale chose to fix the lab's copy of the Online history
# (30/10/2026); the administrator fixes Online by hand (read-only from here).
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
WRONG, RIGHT = 1982350.0, 182350.0
partner = env['res.partner'].sudo().search([('ref', '=', 'ODOO-623')])
assert len(partner) == 1 and 'MUÑOZ MARCELO' in partner.name
mov = env['distri.cuenta.movimiento'].sudo().search([('partner_id', 'child_of', partner.id),
                                                     ('comprobante', '=', 'PBNK1/2026/03007')])
assert len(mov) == 1 and abs(mov.haber - WRONG) < 0.01, mov.mapped('haber')
mirror = env['distri.online.payment'].sudo().search([('name', '=', 'PBNK1/2026/03007')])
assert len(mirror) == 1 and abs(mirror.amount - WRONG) < 0.01
card = env['distri.mp.collection'].sudo().browse(25756)
assert abs(card.amount - RIGHT) < 0.01 and card.source_id == '173895300646'
Review = env['distri.balance.review'].sudo()
before, review_before = partner._distri_account_balance(), Review._distri_lab_balance(partner)
mov.write({'haber': RIGHT,
           'concepto': '%s · corregido en el lab el 30/10/2026: en Online figura $1.982.350 por error de '
                       'tipeo; Mercado Pago (op. 173895300646) $182.350' % (mov.concepto or '')})
mirror.write({'amount': RIGHT})
partner.message_post(body='Pago PBNK1/2026/03007 del 14/08/2026 corregido en la historia: $1.982.350 → '
                          '$182.350 (error de tipeo en Online; Mercado Pago op. 173895300646). '
                          'Falta corregirlo en Online.')
Review._distri_refresh(partner)
env.flush_all()
after, review_after = partner._distri_account_balance(), Review._distri_lab_balance(partner)
print('RESULT saldo %.2f -> %.2f | revision %.2f -> %.2f' % (before, after, review_before, review_after))
ok = abs(after - before - 1800000) < 0.01 and abs(review_after - review_before - 1800000) < 0.01
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED', 'ok' if ok else 'NO CIERRA')
