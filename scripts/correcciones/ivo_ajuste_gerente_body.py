# IVO LAURENTI (ODOO-143): the manager (Granero) confirmed the debt informed
# to the customer, $1.662.819 (ale, 30/10/2026). The lab says $2.262.821:
# the two $1 invoices made only to mark the collector's route (0001/2025/02677,
# 0001/2026/04606, already authorized to cancel) and the $600.000 transfer of
# 15/09 from Ivo's MP account that Online has on PATRICIO MAYORISTA
# (PBNK1/2026/03541). An AJC credit takes the lab to the confirmed figure;
# Patricio is left as it is. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
TARGET = 1662819.0
uenv = env(user=env.ref('base.user_admin').id)
ivo = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-143')])
assert len(ivo) == 1 and ivo.name == 'IVO LAURENTI'
Review = uenv['distri.balance.review'].sudo()
before = Review._distri_lab_balance(ivo)
assert abs(before - 2262821) < 0.01, before
wiz = uenv['distri.balance.adjustment.wizard'].create({
    'partner_id': ivo.id, 'target_balance': TARGET,
    'authorized_by': 'Gerente (saldo informado al cliente)',
    'motive': 'Saldo confirmado por el gerente: $1.662.819. Se descuentan las dos facturas de $1 que solo '
              'marcaban la ruta del cobrador (0001/2025/02677 y 0001/2026/04606) y la transferencia de '
              '$600.000 del 15/09/2026 desde la cuenta de Ivo, que en Online está cargada a Patricio '
              'Mayorista (PBNK1/2026/03541).',
})
wiz.action_apply()
env.flush_all()
adj = uenv['distri.balance.adjustment'].sudo().search([('partner_id', '=', ivo.id)], limit=1)
after, account = Review._distri_lab_balance(ivo), ivo._distri_account_balance()
print('RESULT', adj.name, adj.difference, 'revision %.2f -> %.2f | cuenta %.2f' % (before, after, account))
if DO_COMMIT and abs(after - TARGET) < 0.01 and abs(account - TARGET) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
