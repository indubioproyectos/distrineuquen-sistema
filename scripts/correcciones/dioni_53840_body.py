# Card 50352 ($53.840, 28/10): Simon resent Daiana San Martín's receipt and
# then this one in the same minute, each photo followed by its caption, so the
# photo belongs to «Dionisio romero» (ROMERO DIONI, ODOO-36), not to Daiana.
# Simon's 28/10 sheet shows invoice 07238 $153.840 with a dash in cash: this
# transfer is a partial payment. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
card = uenv['distri.mp.collection'].sudo().browse(50352)
partner = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-36')])
assert len(partner) == 1 and partner.name.startswith('ROMERO DIONI'), partner.mapped('name')
assert card.kind == 'income' and card.state == 'draft' and abs(card.amount - 53840) < 0.01, card.id
assert card.receipt_id.id == 14822 and not card.receipt_id.distri_partner_id, card.receipt_id
before = partner._distri_account_balance()
card.receipt_id.write({'distri_partner_id': partner.id,
                       'distri_partner_source': 'Leyenda «Dionisio romero» (Simon, 28/10 16:09); corregido por ale el 29/10'})
if card.partner_id != partner:
    card.partner_id = partner
card.with_context(distri_skip_confirm_notice=True).action_confirm()
env.flush_all()
after = partner._distri_account_balance()
good = card.state == 'confirmed' and abs(before - after - 53840) < 0.01
print('RESULT %s %s %s saldo %.2f -> %.2f' % (card.state, partner.name, card.payment_id.name, before, after))
if DO_COMMIT and good:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
