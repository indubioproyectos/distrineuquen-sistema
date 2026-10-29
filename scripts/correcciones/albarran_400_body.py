# The $400.000 BNA transfer of 29/09/2026 (card 27146, MP 181394723878) is
# ALBARRÁN GUILLERMO's (Italo's note next to the photo, his usual bank, Online
# order), not ARRATIA KATHY's; checked by a second agent on 28/10. Online
# already has it as PBNK1/2026/03962 (29/09), wrongly linked to card 26410
# (21/09, no receipt). Same steps as relink_online.py of 27/10: the Online
# payment moves to card 27146 (assigned to Albarrán) and 26410 goes back to
# the board. No balance changes. Rolls back unless DO_COMMIT=1.
import os
from odoo import fields

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
C = uenv['distri.mp.collection']
OP = uenv['distri.online.payment'].sudo()
P = uenv['res.partner'].sudo()
albarran = P.browse(16)
assert albarran.ref == 'ODOO-47'
x, y = C.browse(26410), C.browse(27146)
op = OP.search([('collection_id', '=', x.id)])
assert len(op) == 1 and op.name == 'PBNK1/2026/03962' and op.partner_id == albarran, op.mapped('name')
assert y.state == 'draft' and not y.online_payment_id and y.amount == op.amount == 400000
balances = {p.id: p._distri_account_balance() for p in albarran | y.partner_id}
print('ANTES', 'x', x.state, x.online_payment_id.name, '| y', y.state, y.partner_id.name, '| link_kind', op.link_kind)
old = y.partner_id.name
y.sudo().write({'partner_id': albarran.id})
y.sudo().message_post(body='Cliente corregido: %s → %s. La foto BNA la mandó Italo debajo de su nota '
                           '"ALBARRAN GUILLERMO" (29/09 10:59); el comprobante de Arratia de ese día es el '
                           'Supervielle de $157.680.' % (old, albarran.name))
x.action_online_unlink()
if x.receipt_id and (x.receipt_id.distri_receipt_note or '').startswith('Ya cargado en Online'):
    x.receipt_id.sudo().with_context(distri_receipt_matching=True).write({'distri_receipt_note': False})
op.write({'collection_id': y.id, 'link_kind': op.link_kind, 'linked_at': fields.Datetime.now()})
y.sudo()._distri_mark_online(op)
x.sudo().message_post(body='El pago de Online %s pasó a %s (misma fecha, comprobante BNA del grupo): esta '
                           'transferencia vuelve al tablero sin comprobante.' % (op.name, y.name))
(x | y).sudo()._migration_recompute()
env.flush_all()
after = {pid: P.browse(pid)._distri_account_balance() for pid in balances}
print('DESPUES', 'x', x.state, x.online_payment_id.name, x.migration_box, '| y', y.state, y.partner_id.name,
      y.online_payment_id.name, y.ignore_reason, y.migration_box)
print('SALDOS', balances, after)
ok = all(abs(balances[k] - after[k]) < 0.01 for k in balances)
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED', 'saldos ok' if ok else 'SALDOS CAMBIARON')
