# Closes receipt 868 (Simon, 29/08 16:29, «Facundo vega», amount not legible)
# as «Ya registrado»: it is MP collection 2525 (80.500, Migliore Sol, 16:28),
# already in Online as PBNK1/2026/03325 for VEGA FACUNDO. Confirmed by hand
# on 23/10/2026. Same module calls as aplicar_correcciones_body.register().
# Commits only with DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
MATCHING = {'distri_receipt_matching': True}
msg = env['distri.whatsapp.message'].browse(868)
income = env['distri.mp.collection'].browse(2525)
pay = income.online_payment_id
assert msg.exists() and msg.distri_receipt_state in ('pending', 'no_money'), msg.distri_receipt_state
assert income.exists() and income.amount == 80500 and not income.receipt_id, (income.amount, income.receipt_id)
assert pay and pay.name == 'PBNK1/2026/03325' and pay.amount == 80500, (pay.name, pay.amount)
before = env['account.payment'].search_count([])
msg.with_context(**MATCHING).write({'distri_amount': 80500, 'distri_amount_confidence': 'manual'})
income._link_receipt(msg)
partner = pay.partner_id.commercial_partner_id
msg.with_context(**MATCHING).write({
    'distri_partner_id': partner.id,
    'distri_partner_source': 'Cliente del pago de Online %s' % pay.name})
msg._distri_close('registered', 'Ya cargado en Online: %s del %s.' % (
    pay.name, pay.date.strftime('%d/%m/%Y') if pay.date else ''))
income.message_post(body='Comprobante %s unido a mano (confirmado 23/10/2026): ya cargado en Online como %s.' % (
    msg.display_name, pay.name))
assert env['account.payment'].search_count([]) == before
print('RESULT', {'msg': msg.id, 'state': msg.distri_receipt_state, 'partner': partner.name,
                 'income_receipt': income.receipt_id.id, 'online': pay.name})
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
