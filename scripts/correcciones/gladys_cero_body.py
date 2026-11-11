# C - ROMERO GLADYS (ODOO-689): ale confirmed on 30/10/2026 that Gladys and
# Romero Ángel César owe nothing. The $202.550 credit is the $190.900 of
# 08/10 («Romero Angel Cesar - Gladys Zapala»), a purchase of Romero never
# invoiced, plus $11.650 of small August differences. An AJC debit takes the
# account to $0, like the customers left at $0 with the manager (point 7).
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
gladys = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-689')])
assert len(gladys) == 1 and 'ROMERO GLADYS' in gladys.name
Review = uenv['distri.balance.review'].sudo()
before = Review._distri_lab_balance(gladys)
assert abs(before + 202550) < 0.01, before
uenv['distri.balance.adjustment.wizard'].create({
    'partner_id': gladys.id, 'target_balance': 0.0, 'confirm_debit': True,
    'authorized_by': 'ale (30/10/2026)',
    'motive': 'Confirmado que Gladys Romero y Romero Ángel César no deben nada. Queda en 0: los $190.900 del '
              '08/10/2026 («Romero Angel Cesar - Gladys Zapala») son una compra de Romero Ángel César sin '
              'factura, y $11.650 son diferencias chicas de agosto.',
}).action_apply()
env.flush_all()
after, account = Review._distri_lab_balance(gladys), gladys._distri_account_balance()
adj = uenv['distri.balance.adjustment'].sudo().search([('partner_id', '=', gladys.id)], limit=1)
print('RESULT', adj.name, adj.difference, 'revision %.2f -> %.2f | cuenta %.2f' % (before, after, account))
if DO_COMMIT and abs(after) < 0.01 and abs(account) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
