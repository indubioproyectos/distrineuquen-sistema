# CHAPOCHNICOFF SANDRO GERMAN / CHAPO (ODOO-21): MP 145288269467 (12/02/2026,
# $500.000, photo by Italo 14:15) is BNK1/2026/00488 in Online (12/02,
# «ITALO»). The 25/10 old-period pass missed it. Reviewed 29/10/2026.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
partner = uenv['res.partner'].sudo().browse(97)
assert partner.ref == 'ODOO-21', partner.ref
card = uenv['distri.mp.collection'].sudo().search([('source_id', '=', '145288269467'), ('kind', '=', 'income')])
assert len(card) == 1 and card.state == 'draft' and card.partner_id == partner, (card.state, card.partner_id.name)
before = partner._distri_account_balance()
note = 'Ya en Online: BNK1/2026/00488 (12/02/2026); revisado 29/10/2026'
card.write({'state': 'ignored', 'ignore_reason': 'other', 'ignore_note': note})
card.message_post(body='Descartado: %s.' % note)
uenv['distri.balance.review'].sudo()._distri_refresh(partner)
env.flush_all()
after = partner._distri_account_balance()
print('RESULT estado=%s saldo_antes=%.2f saldo_despues=%.2f' % (card.state, before, after))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
