# The unidentified $600.000 bank transfer of 11/09/2026 23:15 (card 26654,
# operation 178604677418) is taken as PATRICIO MAYORISTA's (ODOO-677) payment
# of invoice 0001/2026/05869 (14/09): the $600.000 of 15/09 that Online has
# on Patricio (PBNK1/2026/03541) came from Ivo Laurenti's account and the
# manager counts it for Ivo (AJC/2026/00024). Patricio is a wholesaler kept
# by the manager: the card is set aside with that note, no balance changes
# (ale, 30/10/2026). Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
patricio = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-677')])
assert len(patricio) == 1 and patricio.distri_es_mayorista
card = uenv['distri.mp.collection'].sudo().browse(26654)
assert card.state == 'draft' and not card.partner_id and card.amount == 600000 and card.source_id == '178604677418'
moved = card._distri_set_aside(label='PATRICIO MAYORISTA (ODOO-677): pago de la factura 0001/2026/05869 del 14/09; '
                                     'la transferencia del 15/09 desde la cuenta de Ivo Laurenti es de Ivo (30/10/2026)')
patricio.message_post(body='La transferencia de $ 600.000 del 11/09 a las 23:15 (operación 178604677418) se toma '
                           'como pago de la factura 0001/2026/05869: la del 15/09 (PBNK1/2026/03541) salió de la '
                           'cuenta de Ivo Laurenti y se le cuenta a Ivo. Apartada como mayorista (30/10/2026).')
env.flush_all()
print('RESULT', len(moved), card.state, card.wholesale_label)
if DO_COMMIT and len(moved) == 1:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
