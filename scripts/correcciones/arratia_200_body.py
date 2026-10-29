# The $200.000 transfer of 29/05/2026 with receipt (card 22691, MP
# 160685101685; receipt from Katherine Liseth Arratia Garrido, same operation
# number, sent by Italo) is ARRATIA KATHY's and Online has it as
# PBNK1/2026/01602 (29/05), wrongly linked to card 22690 (payer Fernando Omar
# Mendoza, no receipt). Same steps as albarran_400_body.py: the Online
# payment moves to 22691 and 22690 goes back to the board. No balance
# changes. Rolls back unless DO_COMMIT=1.
import os
from odoo import fields

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
C = uenv['distri.mp.collection']
OP = uenv['distri.online.payment'].sudo()
P = uenv['res.partner'].sudo()
arratia = P.browse(48)
x, y = C.browse(22690), C.browse(22691)
op = OP.search([('collection_id', '=', x.id)])
assert len(op) == 1 and op.name == 'PBNK1/2026/01602' and op.partner_id == arratia, op.mapped('name')
assert y.state == 'draft' and not y.online_payment_id and y.partner_id == arratia and y.amount == op.amount
assert y.receipt_id.distri_operation == '160685101685'
balances = {p.id: p._distri_account_balance() for p in arratia | x.partner_id}
x.action_online_unlink()
if x.receipt_id and (x.receipt_id.distri_receipt_note or '').startswith('Ya cargado en Online'):
    x.receipt_id.sudo().with_context(distri_receipt_matching=True).write({'distri_receipt_note': False})
op.write({'collection_id': y.id, 'link_kind': op.link_kind, 'linked_at': fields.Datetime.now()})
y.sudo()._distri_mark_online(op)
x.sudo().message_post(body='El pago de Online %s pasó a %s (comprobante de Arratia del grupo, mismo número de '
                           'operación): esta transferencia vuelve al tablero sin comprobante.' % (op.name, y.name))
(x | y).sudo()._migration_recompute()
env.flush_all()
after = {pid: P.browse(pid)._distri_account_balance() for pid in balances}
print('DESPUES', 'x', x.state, x.online_payment_id.name, x.migration_box, x.partner_id.name, '| y', y.state,
      y.online_payment_id.name, y.ignore_reason, y.migration_box)
print('SALDOS', balances, after)
ok = all(abs(balances[k] - after[k]) < 0.01 for k in balances)
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED', 'saldos ok' if ok else 'SALDOS CAMBIARON')
