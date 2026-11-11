# CARBALLO ARIEL - DESPENSA CATA Y VALEN (ODOO-190), ale 30/10/2026: the CUIT
# 27-32699977-1 («Carlos acosta») was learned for Carballo by OdooBot on 29/09
# with no receipt; there is no Acosta customer and no note in the group.
# Carballo pays every invoice in instalments from «Vale y cata» and his
# account returns to ~$0 before each purchase, so those transfers are not
# his. The identity goes and his 8 pending cards ($1.122.300) go back to the
# board without a customer. No balance changes. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CUIT = '27326999771'
uenv = env(user=env.ref('base.user_admin').id)
carballo = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-190')])
assert len(carballo) == 1 and 'CARBALLO ARIEL' in carballo.name
identity = uenv['distri.payer.identity'].sudo().search([('kind', '=', 'cuit'), ('value', '=', CUIT)])
assert identity.partner_id == carballo
cards = uenv['distri.mp.collection'].sudo().search([('payer_cuit', '=', CUIT), ('kind', '=', 'income'), ('state', '=', 'draft')])
assert len(cards) == 8 and abs(sum(cards.mapped('amount')) - 1122300) < 0.01 and not cards.mapped('receipt_id')
assert set(cards.mapped('partner_id').ids) <= {carballo.id}
before = carballo._distri_account_balance()
identity.unlink()
cards.write({'partner_id': False, 'proposal': 'none', 'receipt_candidate': False,
             'proposal_note': 'Sin identificar: no era de CARBALLO ARIEL (CUIT de Carlos Acosta aprendido sin '
                              'comprobante; 30/10/2026).'})
carballo.message_post(body='Se quitó el CUIT 27-32699977-1 (Carlos acosta): sus 8 transferencias sin comprobante '
                           '($ 1.122.300) volvieron a «Sin identificar» (30/10/2026).')
cards._migration_recompute()
uenv['distri.balance.review'].sudo()._distri_refresh(carballo)
env.flush_all()
after = carballo._distri_account_balance()
print('RESULT', set(cards.mapped('column')), 'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and not cards.mapped('partner_id') and abs(after - before) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
