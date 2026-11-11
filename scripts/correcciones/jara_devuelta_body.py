# JARA LORENA - DESPENSA LORENA (ODOO-25): MP 108726098005 (22/04/2025,
# $290.400) is the second of two identical transfers 38 seconds apart; only
# the first (109164134502) is in Online. ale confirmed on 29/10/2026 that it
# was returned to the customer. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
partner = uenv['res.partner'].sudo().browse(221)
assert partner.ref == 'ODOO-25', partner.ref
card = uenv['distri.mp.collection'].sudo().search([('source_id', '=', '108726098005'), ('kind', '=', 'income')])
assert len(card) == 1 and card.state == 'draft' and card.partner_id == partner, (card.state, card.partner_id.name)
before = partner._distri_account_balance()
wiz = uenv['distri.mp.ignore'].create({'collection_id': card.id, 'reason': 'returned',
                                       'note': 'Transferencia repetida (la primera es 109164134502); confirmado 29/10/2026'})
wiz.action_ignore()
uenv['distri.balance.review'].sudo()._distri_refresh(partner)
env.flush_all()
after = partner._distri_account_balance()
print('RESULT estado=%s motivo=%s saldo_antes=%.2f saldo_despues=%.2f' % (card.state, card.ignore_reason, before, after))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
