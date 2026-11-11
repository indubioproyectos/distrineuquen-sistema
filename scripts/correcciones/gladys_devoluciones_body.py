# C - ROMERO GLADYS (ODOO-689), ale 30/10/2026: two refunds for missing
# goods sent from the company's Mercado Pago account (13/08 $38.960 and 14/08
# $18.800, «Devolución por faltantes Gladys Zapala») were never recorded, so
# the customer showed that money in credit. An AJC debit of $57.760 adds them.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
gladys = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-689')])
assert len(gladys) == 1 and 'ROMERO GLADYS' in gladys.name
Review = uenv['distri.balance.review'].sudo()
before = Review._distri_lab_balance(gladys)
assert abs(before + 260310) < 0.01, before
assert not uenv['distri.balance.adjustment'].sudo().search_count([('partner_id', '=', gladys.id)])
uenv['distri.balance.adjustment.wizard'].create({
    'partner_id': gladys.id, 'target_balance': round(before + 57760, 2), 'confirm_debit': True,
    'date': '2026-08-14', 'authorized_by': 'ale (revisión de cuentas, 30/10/2026)',
    'motive': 'Devoluciones por faltantes enviadas desde Mercado Pago y no registradas: 13/08/2026 $38.960 '
              '(operación 173719450352) y 14/08/2026 $18.800 (operación 172878354127).',
}).action_apply()
env.flush_all()
after = gladys._distri_account_balance()
adj = uenv['distri.balance.adjustment'].sudo().search([('partner_id', '=', gladys.id)], limit=1)
print('RESULT', adj.name, adj.difference, 'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and abs(after - before - 57760) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
