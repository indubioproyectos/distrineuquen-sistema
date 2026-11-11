# ESPARZA GASTÓN - AUTOSERVICIO PANCHE (ODOO-19): three MP transfers were
# confirmed in the lab (PMP/2026/00097, 00098, 00277) and Online also has
# them as Italo's manual payments (PBNK1/2026/04155, 04156, 04157: same
# amounts, same days). Counted twice, the customer showed $887.460 in
# credit. Like Albarrán's PMP/2025/00008: the confirmations are undone and
# the cards discarded as already in Online (ale, 30/10/2026).
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
C = uenv['distri.mp.collection']
Mov = uenv['distri.cuenta.movimiento'].sudo()
esparza = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-19')])
assert len(esparza) == 1 and 'ESPARZA' in esparza.name
PAIRS = [(27799, 'PMP/2026/00097', 'PBNK1/2026/04155'),
         (27798, 'PMP/2026/00098', 'PBNK1/2026/04156'),
         (27464, 'PMP/2026/00277', 'PBNK1/2026/04157')]
before = esparza._distri_account_balance()
total = 0.0
for cid, pay_name, online in PAIRS:
    card = C.browse(cid)
    assert card.state == 'confirmed' and card.partner_id == esparza and card.payment_id.name == pay_name, cid
    row = Mov.search([('partner_id', '=', esparza.id), ('comprobante', '=', online)])
    assert len(row) == 1 and abs(row.haber - card.amount) < 0.01, (online, row.mapped('haber'))
    total += card.amount
    card.action_undo()
    uenv['distri.mp.ignore'].create({
        'collection_id': card.id, 'reason': 'other',
        'note': 'Ya en Online: %s; %s era doble (30/10/2026)' % (online, pay_name),
    }).action_ignore()
    card.sudo()._migration_recompute()
    print('TARJETA', cid, card.state, card.ignore_note, '| pago', pay_name, card.payment_id.state if card.payment_id else '-')
uenv['distri.balance.review'].sudo()._distri_refresh(esparza)
env.flush_all()
after = esparza._distri_account_balance()
print('RESULT doble=%.2f saldo %.2f -> %.2f' % (total, before, after))
if DO_COMMIT and abs(after - before - total) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
