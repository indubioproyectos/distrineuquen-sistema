# ROMERO DIONI - DESPENSA EL HUERTO DE NANO (ODOO-36), ale 30/10/2026: on
# 27/05 two nameless bank transfers of $100.000 came in. The 10:10 one
# (card 22745) is Dioni's: Italo posted its receipt at 10:11 «ROMERO DIONI».
# Online has one $100.000 payment for Dioni that day (PBNK1/2026/01567), which
# the old cross paired by amount with the 12:13 one (card 22738, no receipt).
# The links are swapped: 22745 is discarded as already in Online, 22738 goes
# back to the board unidentified. Dioni's balance does not change.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
C = uenv['distri.mp.collection']
dioni = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-36')])
mine, other = C.browse(22745), C.browse(22738)
assert mine.state == 'draft' and mine.receipt_id.id == 11727 and mine.amount == 100000
assert other.state == 'ignored' and other.online_payment_id.name == 'PBNK1/2026/01567' and not other.receipt_id
before = dioni._distri_account_balance()
other.action_online_unlink()
other.sudo().write({'partner_id': False, 'proposal': 'none', 'receipt_candidate': False,
                    'proposal_note': 'Sin identificar: transferencia bancaria sin nombre del 27/05 12:13; no era de '
                                     'Dioni Romero (su pago de ese día es el de las 10:10, con comprobante) (30/10/2026).'})
uenv['distri.mp.ignore'].create({
    'collection_id': mine.id, 'reason': 'other',
    'note': 'Ya en Online: PBNK1/2026/01567 (comprobante de Italo del 27/05 10:11 «ROMERO DIONI») (30/10/2026)',
}).action_ignore()
(mine | other).sudo()._migration_recompute()
uenv['distri.balance.review'].sudo()._distri_refresh(dioni)
env.flush_all()
after = dioni._distri_account_balance()
print('RESULT dioni', mine.state, '| otra', other.state, other.column, other.partner_id.name or '-', '| saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and mine.state == 'ignored' and other.state == 'draft' and not other.partner_id and abs(after - before) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
