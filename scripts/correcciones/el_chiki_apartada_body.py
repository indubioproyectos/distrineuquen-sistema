# «multirubro el chiki» (CUIT 20-47974839-0): 2 pending MP transfers
# (13/08/2025 $265.010, 04/09/2025 $100.000) proposed for ALBARRÁN GUILLERMO
# (ODOO-47) only by a payer identity learned on 29/09. They match no invoice
# or payment of his and nobody with that name shows up in the chat. Set aside
# by hand (no wholesale rule) and the learned CUIT removed, as with Aixa
# (ale, 29/10/2026). The JUAN RAMON TAPIA cards stay (point 11 of the doc).
# No balance changes. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CUIT = '20479748390'
uenv = env(user=env.ref('base.user_admin').id)
albarran = uenv['res.partner'].sudo().browse(16)
assert albarran.ref == 'ODOO-47'
cards = uenv['distri.mp.collection'].sudo().search([('kind', '=', 'income'), ('state', '=', 'draft'), ('payer_cuit', '=', CUIT)])
assert len(cards) == 2 and cards.mapped('partner_id') == albarran, (len(cards), cards.mapped('partner_id.name'))
before = albarran._distri_account_balance()
identity = uenv['distri.payer.identity'].sudo().search([('kind', '=', 'cuit'), ('value', '=', CUIT)])
assert identity.partner_id == albarran, identity.mapped('partner_id.name')
identity.unlink()
moved = cards._distri_set_aside(label='multirubro el chiki (CUIT 20-47974839-0): pagador sin identificar, no es Albarrán')
albarran.message_post(body='Las 2 transferencias de «multirubro el chiki» (CUIT 20-47974839-0, $ 365.010) que estaban '
                           'propuestas para este cliente quedaron apartadas: no coinciden con sus facturas ni pagos '
                           '(29/10/2026).')
uenv['distri.balance.review'].sudo()._distri_refresh(albarran)
env.flush_all()
after = albarran._distri_account_balance()
print('RESULT apartadas=%d estados=%s saldo %.2f -> %.2f' % (len(moved), set(cards.mapped('state')), before, after))
if DO_COMMIT and len(moved) == 2 and abs(after - before) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
