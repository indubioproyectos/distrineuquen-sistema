# The $100.000 BNA transfer of 24/10/2025 22:47 (card 43686, Italo's photo of
# 27/10/2025 under "ALBARRAN GUILLERMO") was confirmed by hand on 28/10/2026
# as PMP/2025/00008, but Online already has it: the 7 Online payments of
# $100.000 for ALBARRÁN between 18/10 and 03/11/2025 pair one to one, in
# order, with the 7 notes in the group, which makes this one
# BNK1/2025/01594 (27/10). The confirmation is undone (payment canceled) and
# the card is discarded as already in Online. Albarrán's balance goes up
# exactly 100.000. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
C = uenv['distri.mp.collection']
P = uenv['res.partner'].sudo()
albarran = P.browse(16)
assert albarran.ref == 'ODOO-47'
c = C.browse(43686)
pay = c.payment_id
assert c.state == 'confirmed' and c.partner_id == albarran and c.amount == 100000, (c.state, c.partner_id.name)
assert pay.name == 'PMP/2025/00008', pay.name
before = albarran._distri_account_balance()
total_before = sum(p._distri_account_balance() for p in P.search([]))
c.action_undo()
uenv['distri.mp.ignore'].create({
    'collection_id': c.id, 'reason': 'other',
    'note': 'Ya en Online: BNK1/2025/01594 (27/10/2025); PMP/2025/00008 era doble',
}).action_ignore()
c.sudo()._migration_recompute()
env.flush_all()
after = albarran._distri_account_balance()
total_after = sum(p._distri_account_balance() for p in P.search([]))
print('TARJETA', c.state, c.ignore_reason, c.ignore_note, '| pago', pay.name, pay.state)
print('SALDO ALBARRAN', before, '->', after, '| TOTAL', total_before, '->', total_after)
ok = abs(after - before - 100000) < 0.01 and abs(total_after - total_before - 100000) < 0.01
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED', 'saldos ok' if ok else 'SALDOS NO CIERRAN')
