# Z - BARROS ANGELA (ODOO-518), ale 30/10/2026: her account is at $0. The
# $201.600 credit is the $101.800 refund for missing goods of 08/06
# («Devolución a Angela Barros (zapala) por faltantes», never recorded) and
# $99.800 of payments above the invoices (02/03 $16.200, 28/09 $73.000,
# October $10.600) that correspond to goods never invoiced. An AJC debit
# takes it to $0; Online gets an invoice for that balance.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
angela = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-518')])
assert len(angela) == 1 and 'BARROS ANGELA' in angela.name
Review = uenv['distri.balance.review'].sudo()
before = Review._distri_lab_balance(angela)
assert abs(before + 201600) < 0.01, before
uenv['distri.balance.adjustment.wizard'].create({
    'partner_id': angela.id, 'target_balance': 0.0, 'confirm_debit': True,
    'authorized_by': 'ale (30/10/2026)',
    'motive': 'Confirmado que la cuenta está en 0. Devolución por faltantes del 08/06/2026 no registrada ($101.800) '
              'y pagos por encima de las facturas sin facturar (02/03 $16.200, 28/09 $73.000, octubre $10.600).',
}).action_apply()
env.flush_all()
after, account = Review._distri_lab_balance(angela), angela._distri_account_balance()
adj = uenv['distri.balance.adjustment'].sudo().search([('partner_id', '=', angela.id)], limit=1)
print('RESULT', adj.name, adj.difference, 'revision %.2f -> %.2f | cuenta %.2f' % (before, after, account))
if DO_COMMIT and abs(after) < 0.01 and abs(account) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
