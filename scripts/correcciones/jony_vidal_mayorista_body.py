# JONY VIDAL - MAYORISTA (ODOO-232, «Distribuidora Laime»; payer «mini
# mercado yhandel», CUIT 20-93909267-7) is a wholesaler whose account the
# manager keeps (ale, 30/10/2026): marked distri_es_mayorista (out of the
# debtors and the account review), a wholesale rule for the CUIT, and its 5
# pending MP transfers ($6.819.500) set aside. The CUIT identity learned for
# the customer goes (a wholesaler is no customer's payer). The transfer
# already confirmed on 02/04 (PMP, $1.289.000) is left as it is.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CUIT = '20939092677'
uenv = env(user=env.ref('base.user_admin').id)
jony = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-232')])
assert len(jony) == 1 and 'JONY VIDAL' in jony.name
C = uenv['distri.mp.collection'].sudo()
cards = C.search([('kind', '=', 'income'), ('state', '=', 'draft'), ('payer_cuit', '=', CUIT)])
assert len(cards) == 5 and abs(sum(cards.mapped('amount')) - 6819500) < 0.01, (len(cards), sum(cards.mapped('amount')))
assert set(cards.mapped('partner_id').ids) <= {jony.id}
before = jony._distri_account_balance()
jony.distri_es_mayorista = True
identity = uenv['distri.payer.identity'].sudo().search([('kind', '=', 'cuit'), ('value', '=', CUIT)])
assert identity.partner_id == jony, identity.mapped('partner_id.name')
identity.unlink()
Rule = uenv['distri.mp.wholesale.rule'].sudo().with_context(active_test=False)
rule = Rule.search([('cuit', '=', CUIT)]) or Rule.create({
    'cuit': CUIT, 'label': 'Jony Vidal - Distribuidora Laime (mayorista)',
    'note': 'Mayorista según ale, 30/10/2026: la cuenta la lleva el gerente.'})
rule._compute_conflict()
moved = cards._distri_set_aside(rule=rule, auto=False)
jony.message_post(body='Marcado como mayorista: la cuenta la lleva el gerente. Sus %d transferencias pendientes '
                       '(CUIT 20-93909267-7) quedaron apartadas como mayorista (30/10/2026).' % len(moved))
uenv['distri.balance.review'].sudo()._distri_refresh(jony)
env.flush_all()
after = jony._distri_account_balance()
print('RESULT apartadas=%d estados=%s regla=%s conflicto=%s saldo %.2f -> %.2f' % (
    len(moved), set(cards.mapped('state')), rule.label, rule.conflict, before, after))
if DO_COMMIT and not rule.conflict and len(moved) == 5 and abs(after - before) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
