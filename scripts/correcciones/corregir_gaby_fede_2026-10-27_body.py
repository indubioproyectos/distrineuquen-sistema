# odoo shell body (27/10/2026, decided by ale). Dry run unless DO_COMMIT=1.
#   1. card 27467: Kiosco Gaby MP $1.000.000 of 15/10 was linked by amount to the
#      Online cash payment of AGENCIA 107 (PBNK1/2026/04135, Jorge's planilla of
#      15/10). They are different payments: unlink it and set it aside under the
#      Kiosco Gaby rule (a customer of Jorge Ustia).
#   2. cards 26352 and 21618: FEDE SPEED's account (CUIT 20368165884), no receipt,
#      proposed for two retail customers: set them aside under the FEDE rule.
# Usage:
#   NAME=distri-correccion scripts/l10n_ar/lab.sh odoo shell -d distri_lab --no-http \
#     --log-level=warn < scripts/correcciones/corregir_gaby_fede_2026-10-27_body.py
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
C = env['distri.mp.collection'].sudo()
Rule = env['distri.mp.wholesale.rule'].sudo()


def show(cards):
    return [(c.id, c.state, c.amount, c.partner_id.display_name or '', c.wholesale_label or '',
             c.online_payment_id.name or '') for c in cards]


gaby_rule = Rule.search([('cuit', '=', '27273685796'), ('active', '=', True)], limit=1)
fede_rule = Rule.search([('cuit', '=', '20368165884'), ('active', '=', True)], limit=1)
assert gaby_rule and fede_rule, 'missing wholesale rule'

gaby = C.browse(27467)
assert gaby.exists() and gaby.payer_cuit == '27273685796' and gaby.amount == 1000000
assert gaby.state == 'ignored' and gaby.ignore_reason == 'online'
assert gaby.online_payment_id.name == 'PBNK1/2026/04135'
fede = C.browse([26352, 21618])
assert len(fede.exists()) == 2
assert all(c.payer_cuit == '20368165884' and c.state == 'draft' and not c.receipt_id for c in fede)

print('BEFORE', show(gaby | fede))
gaby.action_online_unlink()
moved = gaby._distri_set_aside(rule=gaby_rule)
moved |= fede._distri_set_aside(rule=fede_rule)
env.flush_all()
print('AFTER ', show(gaby | fede))
print('SET ASIDE', len(moved), 'of 3')
if DO_COMMIT and len(moved) == 3:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED' + ('' if len(moved) == 3 else ' (expected 3 set aside)'))
