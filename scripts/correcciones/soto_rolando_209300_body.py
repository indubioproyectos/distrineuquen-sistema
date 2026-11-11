# SOTO ROLANDO - CARREFOUR HIPERMERCADO (ODOO-100): the $209.300 Mercado
# Pago transfer of 18/09/2026 from his usual account (Gabriela Sandoval, CUIT
# 27-20237603-2) pays invoice 0001/2026/06055 of the same day, exact amount.
# It is the only one of his transfers with no «Rolando soto» note in the
# WhatsApp group. Approved without receipt on ale's word (30/10/2026), the
# board's own «Aprobar sin comprobante». Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CUIT = '27202376032'
NOTE = ('Lo confirmó ale el 30/10/2026: transferencia desde su cuenta de siempre (Gabriela Sandoval) por el '
        'importe exacto de la factura 0001/2026/06055 del mismo día')
uenv = env(user=env.ref('base.user_admin').id)
partner = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-100')])
assert len(partner) == 1 and partner.name.startswith('SOTO ROLANDO')
Card = uenv['distri.mp.collection'].sudo()
others = Card.search([('payer_cuit', '=', CUIT), ('kind', '=', 'income'), ('partner_id', 'not in', [False, partner.id])])
assert not others, others.mapped('partner_id.name')
card = Card.browse(26446)
assert card.kind == 'income' and card.state == 'draft' and card.payer_cuit == CUIT and card.partner_id == partner
assert abs(card.amount - 209300) < 0.01 and card.receipt_candidate and not card.receipt_id
before = partner._distri_account_balance()
uenv['distri.mp.vouch'].sudo().create({'collection_id': card.id, 'note': NOTE}).action_apply()
env.flush_all()
after = partner._distri_account_balance()
print('RESULT', card.state, card.payment_id.name, 'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and card.state == 'confirmed' and abs(before - after - 209300) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
