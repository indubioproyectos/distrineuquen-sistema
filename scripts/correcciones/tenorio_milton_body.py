# TENORIO MILTON - AUTOSERVICIO MYS (ODOO-164), ale 30/10/2026:
# - The CUIT 27-94321699-7 («YOVANA APARICIO ROMERO») was learned for Milton
#   by OdooBot on 29/09 with no receipt; X - YOVANA - CENTENARIO pays from
#   other accounts and is settled. The identity goes, a wholesale rule for the
#   CUIT is created (the 25/10 cross already set one aside as «pagador
#   mayorista») and her 5 pending transfers ($630.100) are set aside.
# - The $79.700 transfer of 14/05 from his own account «M&S» (card 23056) is
#   approved without receipt.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
YOV = '27943216997'
uenv = env(user=env.ref('base.user_admin').id)
milton = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-164')])
assert len(milton) == 1 and milton.name.startswith('TENORIO MILTON')
C = uenv['distri.mp.collection'].sudo()
before = milton._distri_account_balance()
identity = uenv['distri.payer.identity'].sudo().search([('kind', '=', 'cuit'), ('value', '=', YOV)])
assert identity.partner_id == milton
identity.unlink()
yov = C.search([('kind', '=', 'income'), ('state', '=', 'draft'), ('payer_cuit', '=', YOV)])
assert len(yov) == 5 and abs(sum(yov.mapped('amount')) - 630100) < 0.01, (len(yov), sum(yov.mapped('amount')))
Rule = uenv['distri.mp.wholesale.rule'].sudo().with_context(active_test=False)
rule = Rule.search([('cuit', '=', YOV)]) or Rule.create({
    'cuit': YOV, 'label': 'Yovana Aparicio Romero (pagador mayorista)',
    'note': 'No es de Tenorio Milton ni de X - Yovana - Centenario (ale, 30/10/2026).'})
rule._compute_conflict()
moved = yov._distri_set_aside(rule=rule, auto=False)
card = C.browse(23056)
assert card.state == 'draft' and card.partner_id == milton and abs(card.amount - 79700) < 0.01
assert card.receipt_candidate and not card.receipt_id
uenv['distri.mp.vouch'].sudo().create({'collection_id': card.id, 'note':
    'Lo confirmó ale el 30/10/2026: transferencia desde su cuenta «M&S»'}).action_apply()
milton.message_post(body='Se quitó el CUIT 27-94321699-7 (Yovana Aparicio Romero): sus 5 transferencias quedaron '
                         'apartadas como mayorista. Se aprobó sin comprobante la transferencia de $ 79.700 del 14/05 '
                         'desde su cuenta «M&S» (30/10/2026).')
uenv['distri.balance.review'].sudo()._distri_refresh(milton)
env.flush_all()
after = milton._distri_account_balance()
print('RESULT apartadas=%d conflicto=%s | 79700 %s %s | saldo %.2f -> %.2f' % (
    len(moved), rule.conflict, card.state, card.payment_id.name, before, after))
if DO_COMMIT and len(moved) == 5 and not rule.conflict and card.state == 'confirmed' and abs(before - after - 79700) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
