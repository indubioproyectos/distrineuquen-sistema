# SERGIO GEREGA: the $1.770.320 transfer of 29/10 19:54 (card 50416) came in
# report 110 and was confirmed, but its WhatsApp receipt (message 14843,
# «Sergio gerega (- $1.100 ...») stayed as a loose «Sin transferencia» card
# (50451). The receipt is tied to the income and the loose card closes, the
# board's own _link_receipt. No balance changes (ale, 30/10/2026).
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
C = env['distri.mp.collection'].sudo()
income, loose = C.browse(50416), C.browse(50451)
receipt = env['distri.whatsapp.message'].sudo().browse(14843)
assert income.state == 'confirmed' and abs(income.amount - 1770320) < 0.01 and 'GEREGA' in income.partner_id.name
assert loose.kind == 'receipt' and loose.state == 'draft' and loose.receipt_id == receipt
print('RESULT antes', income.receipt_id.id, loose.state)
assert not income.receipt_id or income.receipt_id == receipt, income.receipt_id
before = income.partner_id._distri_account_balance()
income._link_receipt(receipt)
(income | loose)._migration_recompute()
env.flush_all()
after = income.partner_id._distri_account_balance()
print('RESULT despues', income.receipt_id.id, loose.state, loose.ignore_note, 'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and loose.state == 'ignored' and income.receipt_id == receipt and abs(after - before) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
