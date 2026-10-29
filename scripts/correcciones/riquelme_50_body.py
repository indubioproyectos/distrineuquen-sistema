# The $50.000 transfer of 25/03/2026 with receipt (card 21753, MP
# 151091279021; sent from Italo's own account with his note "RIQUELME
# VANESA") is RIQUELME VANESA's and Online has it as PBNK1/2026/00558
# (25/03), wrongly linked to card 46390 (16/03, no receipt). Same steps as
# arratia_200_body.py. No balance changes. Rolls back unless DO_COMMIT=1.
import os
from odoo import fields

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
C = uenv['distri.mp.collection']
OP = uenv['distri.online.payment'].sudo()
P = uenv['res.partner'].sudo()
riquelme = P.browse(420)
x, y = C.browse(46390), C.browse(21753)
op = OP.search([('collection_id', '=', x.id)])
assert len(op) == 1 and op.name == 'PBNK1/2026/00558' and op.partner_id == riquelme, op.mapped('name')
assert y.state == 'draft' and not y.online_payment_id and y.partner_id == riquelme and y.amount == op.amount
assert y.receipt_id.distri_operation == '151091279021'
balances = {p.id: p._distri_account_balance() for p in riquelme | x.partner_id}
x.action_online_unlink()
if x.receipt_id and (x.receipt_id.distri_receipt_note or '').startswith('Ya cargado en Online'):
    x.receipt_id.sudo().with_context(distri_receipt_matching=True).write({'distri_receipt_note': False})
op.write({'collection_id': y.id, 'link_kind': op.link_kind, 'linked_at': fields.Datetime.now()})
y.sudo()._distri_mark_online(op)
x.sudo().message_post(body='El pago de Online %s pasó a %s (comprobante del 25/03 que Italo mandó al grupo con la nota '
                           '"RIQUELME VANESA"): esta transferencia vuelve al tablero sin comprobante.' % (op.name, y.name))
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
