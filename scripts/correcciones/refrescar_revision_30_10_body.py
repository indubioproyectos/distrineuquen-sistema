# Rebuilds the «Revisión por cliente» rows (stored) of every customer touched
# on 30/10/2026: some approvals were made without refreshing the review, so
# the screen kept the old balance (e.g. Manganaro 231.120 instead of -30.680).
# Derived data only: no payment or move changes. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
REFS = [19, 100, 437, 164, 90, 40, 593, 370, 36, 80, 231, 134, 89, 81, 316, 181, 463, 689, 700, 644, 518,
        145, 29, 119, 281, 655, 777, 531, 61, 459, 78, 242, 85, 352, 623, 143, 232, 677, 152, 561, 50, 33]
P = env['res.partner'].sudo()
partners = P.search([('ref', 'in', ['ODOO-%d' % r for r in REFS])])
Review = env['distri.balance.review'].sudo()
Review._distri_refresh(partners)
env.flush_all()
bad = []
for p in partners:
    row = Review.search([('partner_id', '=', p.id)], limit=1)
    acct = p._distri_account_balance()
    if row and abs(row.lab_balance - Review._distri_lab_balance(p)) > 0.01:
        bad.append(p.ref)
    if p.ref in ('ODOO-281', 'ODOO-100', 'ODOO-145'):
        print('RESULT', p.ref, p.name[:35], 'revision', row.lab_balance if row else None, 'cuenta', acct)
print('RESULT clientes', len(partners), 'desfasados', bad)
if DO_COMMIT and not bad:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
