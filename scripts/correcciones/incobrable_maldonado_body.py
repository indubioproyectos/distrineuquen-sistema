# Marks MALDONADO CLAUDIO (res.partner 291, ODOO-95) as «Incobrable»
# (19.0.1.47.0, models/deudores.py): he leaves «Deudores» and its totals and
# shows in «Deudas incobrables». Nothing is written off: no accounting entry,
# the balance stays. Decided by management on 28/10/2026.
#   NAME=distri-incobrable DO_COMMIT=1 scripts/l10n_ar/lab.sh odoo shell -d distri_lab \
#     --no-http --log-level=warn < scripts/correcciones/incobrable_maldonado_body.py
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
P = env['res.partner'].sudo().with_context(active_test=False)


def totals():
    """(net sum, sum of the debts) of every commercial partner's balance."""
    partners = P.search([('parent_id', '=', False), ('distri_generic_partner', '=', False)])
    balances = [p._distri_account_balance() for p in partners]
    return round(sum(balances), 2), round(sum(b for b in balances if b > 0), 2)


partner = P.browse(291)
assert partner.exists() and partner.ref == 'ODOO-95' and partner.name == 'MALDONADO CLAUDIO', (
    partner.ref, partner.name)
before_total = totals()
before = round(partner._distri_account_balance(), 2)
moves_before = env['account.move'].search_count([])
partner.write({
    'distri_uncollectible': True,
    'distri_uncollectible_date': '2026-10-28',
    'distri_uncollectible_note': 'Dejó de pagar en nov/2025; incobrable según la gerencia',
})
after = round(partner._distri_account_balance(), 2)
after_total = totals()
assert before == after, (before, after)
assert before_total == after_total, (before_total, after_total)
assert env['account.move'].search_count([]) == moves_before
print('RESULT', {'partner': partner.name, 'balance': after, 'uncollectible': partner.distri_uncollectible,
                 'date': str(partner.distri_uncollectible_date), 'totals_net_debts': after_total})
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
