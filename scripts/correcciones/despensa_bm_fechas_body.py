# DESPENSA BM (ODOO-476), ale 30/10/2026: two Online payments carry the wrong
# date (amounts are right). PBNK1/2026/00707 ($154.000) says 03/02 but it is
# the MP transfer of 03/04 (op. 153174914066; with $250.000 cash it pays
# invoice 02046 of 03/04; the customer was created on 09/02). PBNK1/2026/02415
# ($305.980) says 18/07 but it is the MP transfer of 16/06 (op. 164406978124;
# with $200.000 cash it pays invoice 03488 of 16/06). The lab's copy of the
# history gets the right dates; the administrator fixes Online.
# Rolls back unless DO_COMMIT=1.
import os
from datetime import date

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
Mov = env['distri.cuenta.movimiento'].sudo()
bm = env['res.partner'].sudo().search([('ref', '=', 'ODOO-476')])
FIX = [('PBNK1/2026/00707', date(2026, 2, 3), date(2026, 4, 3), 154000.0),
       ('PBNK1/2026/02415', date(2026, 7, 18), date(2026, 6, 16), 305980.0)]
before = bm._distri_account_balance()
for name, old, new, amount in FIX:
    row = Mov.search([('partner_id', '=', bm.id), ('comprobante', '=', name)])
    assert len(row) == 1 and row.fecha == old and abs(row.haber - amount) < 0.01, name
    row.write({'fecha': new, 'concepto': '%s · fecha corregida en el lab el 30/10/2026 (en Online figura %s)'
               % (row.concepto or '', old.strftime('%d/%m/%Y'))})
    mirror = env['distri.online.payment'].sudo().search([('name', '=', name)])
    if len(mirror) == 1:
        mirror.write({'date': new})
    print('RESULT', name, old, '->', new)
env['distri.balance.review'].sudo()._distri_refresh(bm)
env.flush_all()
after = bm._distri_account_balance()
print('RESULT saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and abs(after - before) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
