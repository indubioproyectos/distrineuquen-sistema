# SANDOVAL JOSI - DESPENSA EMI (ODOO-29), ale 30/10/2026: the CUIT
# 27-95482583-9 («Martina Salinas Morales») was learned for Josi by OdooBot
# on 29/09 with no receipt. Her two transfers (24/04 $100.000 and 01/09
# $45.000) have no photo or note in the group, while every payment of Josi
# comes from her own account with a «SANDOVAL JOSI» note. The identity goes
# and both cards go back to the board without a customer. No balance changes.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CUIT = '27954825839'
uenv = env(user=env.ref('base.user_admin').id)
josi = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-29')])
assert len(josi) == 1 and 'SANDOVAL JOSI' in josi.name
identity = uenv['distri.payer.identity'].sudo().search([('kind', '=', 'cuit'), ('value', '=', CUIT)])
assert identity.partner_id == josi
cards = uenv['distri.mp.collection'].sudo().search([('payer_cuit', '=', CUIT), ('kind', '=', 'income'), ('state', '=', 'draft')])
assert len(cards) == 2 and set(cards.mapped('partner_id').ids) == {josi.id} and not cards.mapped('receipt_id')
before = josi._distri_account_balance()
identity.unlink()
cards.write({'partner_id': False, 'proposal': 'none', 'receipt_candidate': False,
             'proposal_note': 'Sin identificar: no era de SANDOVAL JOSI (CUIT de Martina Salinas Morales aprendido '
                              'sin comprobante; 30/10/2026).'})
josi.message_post(body='Se quitó el CUIT 27-95482583-9 (Martina Salinas Morales): sus transferencias del 24/04 '
                       '($ 100.000) y 01/09 ($ 45.000) no tienen comprobante y volvieron a «Sin identificar» (30/10/2026).')
cards._migration_recompute()
uenv['distri.balance.review'].sudo()._distri_refresh(josi)
env.flush_all()
after = josi._distri_account_balance()
print('RESULT', cards.mapped('column'), cards.mapped('partner_id.name'), 'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and not cards.mapped('partner_id') and abs(after - before) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
