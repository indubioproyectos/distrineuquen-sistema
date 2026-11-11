# MANGANARO VALENTIN - MARIA PAZ - AUTOSERVICIO GENOVES (ODOO-281): the
# $261.800 Mercado Pago transfer of 23/06/2026 from his account «Mercado
# Genovés» pays invoice 0001/2026/03668 of the same day, exact amount, with no
# receipt in the group. Approved without receipt on ale's word (30/10/2026).
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
partner = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-281')])
assert len(partner) == 1 and 'MANGANARO' in partner.name
card = uenv['distri.mp.collection'].sudo().browse(23645)
assert card.state == 'draft' and card.partner_id == partner and abs(card.amount - 261800) < 0.01
assert card.payer_cuit == '20367844095' and not card.receipt_id
before = partner._distri_account_balance()
if card.receipt_candidate:
    uenv['distri.mp.vouch'].sudo().create({'collection_id': card.id, 'note':
        'Lo confirmó ale el 30/10/2026: transferencia desde su cuenta «Mercado Genovés» por el importe exacto de la '
        'factura 0001/2026/03668 del mismo día'}).action_apply()
else:
    card.with_context(distri_skip_confirm_notice=True).action_confirm()
env.flush_all()
after = partner._distri_account_balance()
print('RESULT', card.state, card.payment_id.name, 'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and card.state == 'confirmed' and abs(before - after - 261800) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
