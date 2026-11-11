# ARNEZ ALBINA - DESPENSA CONCORDIA (ODOO-151), ale 31/10/2026: Jorge's sheet
# of 07/10 has invoice 0001/2026/06585 ($316.000) collected in full in cash.
# In Online that payment is a DRAFT dated 06/10 (id 6344, created 08/10),
# never posted. Every other invoice of the customer is paid exactly
# (06861 = $100.000 cash + $162.100 Mercado Pago; 07011 and 07240 by Mercado
# Pago), so 06585 is the only one without payment. Loaded here as cash, the
# same way as the sheet loader (scripts/planillas-2026-10-28), which would
# skip it because of the Online draft. Rolls back unless DO_COMMIT=1.
import os
from datetime import date

from odoo.addons.distri_pedido.models.cobros import AMOUNT_TOLERANCE

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
KEY = 'distri_cobro_ef_20261007_151_0001202606585'
Payment = env['account.payment'].sudo()
partner = env['res.partner'].sudo().search([('ref', '=', 'ODOO-151')])
assert len(partner) == 1 and 'ARNEZ' in partner.name
journal = env['account.journal'].sudo().search([('code', '=', 'CSH1'), ('type', '=', 'cash'),
                                                ('company_id', '=', env.company.id)])
assert len(journal) == 1
assert not Payment.search_count([('payment_reference', '=', KEY)]), 'ya cargado'
same = Payment.search([('partner_id', 'child_of', partner.id), ('payment_type', '=', 'inbound'),
                       ('state', '!=', 'canceled')]).filtered(lambda p: abs(p.amount - 316000) <= AMOUNT_TOLERANCE)
assert not same, same.mapped('name')
before = partner._distri_account_balance()
payment = Payment.create({
    'payment_type': 'inbound', 'partner_type': 'customer', 'partner_id': partner.id,
    'amount': 316000.0, 'journal_id': journal.id, 'date': date(2026, 10, 7),
    'memo': 'Efectivo · cobró JORGE · planilla 07/10 · 0001/2026/06585 · cargado el 31/10/2026 al cruzar las '
            'planillas de efectivo 2026: en Online quedó en borrador (fecha 06/10, sin confirmar)',
    'payment_reference': KEY, 'company_id': env.company.id,
})
payment.action_post()
env['distri.route'].sudo()._apply_oldest_first(payment, partner)
partner.message_post(body='Efectivo de $ 316.000 del 07/10 (planilla de Jorge, factura 0001/2026/06585) cargado '
                          'el 31/10/2026: en Online estaba en borrador sin confirmar.')
env['distri.balance.review'].sudo()._distri_refresh(partner)
env.flush_all()
after = partner._distri_account_balance()
print('RESULT', payment.name, payment.state, 'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and abs(before - after - 316000) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
