# «La suerte» (Matías Martínez, CUIT 20-29547394-1) is a wholesaler, as ale
# confirmed on 29/10/2026. Its 37 pending MP transfers (08/2025-10/2026,
# $29.095.457) were proposed for GOMEZ JULIO - ESTACIÓN SHELL (ODOO-74) only
# because the CUIT was "known" there (payer identity learned on 29/09). A wholesale rule for the CUIT sets them
# aside («Mayorista / fuera del sistema») and catches the next imports. The
# one already linked to Gomez's Online PBNK1/2026/02447 is left as it is (to
# review with the administration). No balance changes. Rolls back unless
# DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CUIT = '20295473941'
uenv = env(user=env.ref('base.user_admin').id)
gomez = uenv['res.partner'].sudo().browse(194)
assert gomez.ref == 'ODOO-74'
C = uenv['distri.mp.collection'].sudo()
cards = C.search([('kind', '=', 'income'), ('state', '=', 'draft'), ('payer_cuit', '=', CUIT)])
assert len(cards) == 37 and set(cards.mapped('partner_id').ids) == {gomez.id}, (len(cards), cards.mapped('partner_id.name'))
total = sum(cards.mapped('amount'))
before = gomez._distri_account_balance()
# the CUIT was learned for Gomez on 29/09 (payer identity «La suerte»): a
# wholesaler is no customer's payer, and the rule stays in conflict otherwise
identity = uenv['distri.payer.identity'].sudo().search([('kind', '=', 'cuit'), ('value', '=', CUIT)])
assert identity.partner_id == gomez, identity.mapped('partner_id.name')
identity.unlink()
Rule = uenv['distri.mp.wholesale.rule'].sudo().with_context(active_test=False)
rule = Rule.search([('cuit', '=', CUIT)])
if not rule:
    rule = Rule.create({'cuit': CUIT, 'label': 'La Suerte - Matías Martínez (mayorista)',
                        'note': 'Mayorista según ale, 29/10/2026. Sus transferencias estaban propuestas '
                                'para GOMEZ JULIO - ESTACIÓN SHELL solo por el CUIT.'})
rule._compute_conflict()
print('REGLA', rule.id, rule.label, 'conflicto=%s %s' % (rule.conflict, rule.conflict_note or ''))
moved = cards._distri_set_aside(rule=rule, auto=False)
gomez.message_post(body='Las %d transferencias de «La suerte» (CUIT 20-29547394-1, mayorista) que estaban '
                        'propuestas para este cliente quedaron apartadas como mayorista (29/10/2026).' % len(moved))
uenv['distri.balance.review'].sudo()._distri_refresh(gomez)
env.flush_all()
after = gomez._distri_account_balance()
print('RESULT apartadas=%d importe=%.2f estados=%s saldo_gomez %.2f -> %.2f' % (
    len(moved), total, set(cards.mapped('state')), before, after))
if DO_COMMIT and not rule.conflict and len(moved) == 37 and abs(after - before) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
