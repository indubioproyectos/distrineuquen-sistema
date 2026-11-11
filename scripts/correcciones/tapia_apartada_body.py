# JUAN RAMON TAPIA (CUIT 23-31086314-9): 8 Mercado Pago transfers ($1.260.000)
# proposed for ALBARRÁN GUILLERMO (ODOO-47) only by the payer's CUIT. The
# administrator answered on 29/10/2026 (point 11): nobody knows Tapia, the
# payments are not Albarrán's. Set aside like «multirubro el chiki» and the
# learned CUIT removed. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CUIT = '23310863149'
uenv = env(user=env.ref('base.user_admin').id)
albarran = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-47')])
assert len(albarran) == 1
cards = uenv['distri.mp.collection'].sudo().search([('kind', '=', 'income'), ('payer_cuit', '=', CUIT),
                                                   ('state', 'in', ['draft'])])
print('CARDS', [(c.id, str(c.date.date()), c.amount, c.partner_id.ref) for c in cards])
assert len(cards) == 8 and abs(sum(cards.mapped('amount')) - 1260000) < 0.01, (len(cards), sum(cards.mapped('amount')))
assert set(cards.mapped('partner_id').ids) <= {albarran.id}, cards.mapped('partner_id.name')
before = albarran._distri_account_balance()
identity = uenv['distri.payer.identity'].sudo().search([('kind', '=', 'cuit'), ('value', '=', CUIT)])
identity.unlink()
moved = cards._distri_set_aside(label='JUAN RAMON TAPIA (CUIT 23-31086314-9): no es cliente; según Administración no son pagos de Albarrán (29/10/2026)')
albarran.message_post(body='Las 8 transferencias de JUAN RAMON TAPIA ($ 1.260.000) quedaron apartadas: según Administración '
                           'no son de este cliente (29/10/2026).')
uenv['distri.balance.review'].sudo()._distri_refresh(albarran)
env.flush_all()
after = albarran._distri_account_balance()
print('RESULT apartadas=%d estados=%s saldo %.2f -> %.2f' % (len(moved), set(cards.mapped('state')), before, after))
if DO_COMMIT and len(moved) == 8 and abs(after - before) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
