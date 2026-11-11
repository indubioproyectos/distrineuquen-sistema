# GARRIDO VICTOR VALENTINA SUR (ODOO-561) buys at the warehouse and always
# pays on the spot; his payments were never loaded (ale, 30/10/2026). The
# invoices 06241 (25/09, $158.200) and 06925 (16/10, $193.400) are paid in
# cash at the warehouse on the invoice day (CSH1, like Juan Bordón's
# PCSH1/2026/00232); 07276 (30/10, $228.400) stays open. His 4 unnamed
# candidate transfers without receipt ($195.100) are sales that were never
# recorded, so they are set aside instead of leaving a credit.
# Rolls back unless DO_COMMIT=1.
import os
from datetime import date

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
INVOICES = [('0001/2026/06241', date(2026, 9, 25), 158200.0),
            ('0001/2026/06925', date(2026, 10, 16), 193400.0)]
uenv = env(user=env.ref('base.user_admin').id)
Payment = uenv['account.payment'].sudo()
Route = uenv['distri.route'].sudo()
partner = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-561')])
assert len(partner) == 1 and 'GARRIDO' in partner.name, partner.mapped('name')
journal = uenv['account.journal'].sudo().search([('code', '=', 'CSH1'), ('type', '=', 'cash'),
                                                 ('company_id', '=', env.company.id)])
assert len(journal) == 1
before = partner._distri_account_balance()

cards = uenv['distri.mp.collection'].sudo().search([('kind', '=', 'income'), ('partner_id', '=', partner.id),
                                                   ('state', '=', 'draft')])
print('CARDS', [(c.id, str(c.date.date()), c.amount) for c in cards])
assert len(cards) == 4 and abs(sum(cards.mapped('amount')) - 195100) < 0.01, (len(cards), sum(cards.mapped('amount')))

loaded = Payment.browse()
for number, day, amount in INVOICES:
    # the invoices live only in the imported Online history
    inv = uenv['distri.cuenta.movimiento'].sudo().search([('partner_id', 'child_of', partner.id),
                                                          ('tipo', '=', 'factura'), ('comprobante', '=', number)])
    assert len(inv) == 1 and inv.fecha == day and abs(inv.debe - amount) < 0.01, number
    key = 'distri_cobro_dep_%s_ODOO-561_%s' % (day.strftime('%Y%m%d'), number[-5:])
    assert not Payment.search_count([('payment_reference', '=', key)]), key
    pay = Payment.create({
        'payment_type': 'inbound', 'partner_type': 'customer', 'partner_id': partner.id,
        'amount': amount, 'journal_id': journal.id, 'date': day,
        'memo': 'Efectivo · cobrado en el depósito · %s' % number,
        'payment_reference': key, 'company_id': env.company.id,
    })
    pay.action_post()
    Route._apply_oldest_first(pay, partner)
    loaded |= pay
    print('PAGO', pay.name, day, amount, number)

moved = cards._distri_set_aside(label='GARRIDO VICTOR (ODOO-561): compra en el depósito y paga en el momento; '
                                      'transferencias sin comprobante = ventas no registradas (30/10/2026)')
partner.message_post(body='Facturas 06241 y 06925 pagadas en efectivo en el depósito (compra y paga en el momento). '
                          'Las 4 transferencias sin comprobante ($ 195.100) quedaron apartadas: son ventas no '
                          'registradas (30/10/2026).')
uenv['distri.balance.review'].sudo()._distri_refresh(partner)
env.flush_all()
after = partner._distri_account_balance()
print('RESULT pagos=%s apartadas=%d saldo %.2f -> %.2f' % (loaded.mapped('name'), len(moved), before, after))
if DO_COMMIT and len(loaded) == 2 and len(moved) == 4 and abs(after - 228400) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
