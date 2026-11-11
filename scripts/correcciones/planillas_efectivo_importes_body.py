# Cash-sheet cross (planillas de efectivo 2026), ale 31/10/2026: six Online
# cash payments were typed with the wrong amount. The sheet amount is backed
# by the cash handed in that day (REND = column sum) and closes the invoice
# exactly, alone or with a same-day transfer (checked again by a blind agent).
# The lab's copy of the history (and the payments mirror) gets the sheet
# amount; the administrator fixes Online. Vega Valeria's $5.000 of 10/06
# stays: the sheet's $50.000 is the mistake there.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
P = env['res.partner'].sudo()
Mov = env['distri.cuenta.movimiento'].sudo()
FIX = [('ODOO-81', 'CHANO', 'PCSH1/2026/02121', 348000.0, 34800.0,
        'planilla de Jorge del 30/09; con la transferencia de $70.000 paga la factura 06359 de $104.800'),
       ('ODOO-476', 'DESPENSA BM', 'PCSH1/2026/01266', 391950.0, 362000.0,
        'planilla de Simón del 21/07; paga la factura de $361.950'),
       ('ODOO-386', 'PALACIOS', 'PCSH1/2026/00494', 81.0, 81000.0,
        'planilla de Simón del 13/04; paga la factura 02058 de $81.000'),
       ('ODOO-184', 'FERNANDEZ LORENA', 'PCSH1/2026/02055', 12100.0, 102100.0,
        'planilla de Italo del 25/09; paga la factura 06209 de $102.100'),
       ('ODOO-672', 'MORINIGO', 'PCSH1/2026/01418', 50000.0, 70000.0,
        'planilla de Simón del 30/07; con la transferencia de $102.500 paga la factura de $172.500'),
       ('ODOO-186', 'ALIAS JOSE OMAR', 'PCSH1/2026/01745', 202200.0, 208200.0,
        'planilla de Simón del 31/08; paga la factura 05377 de $208.200')]
money = lambda v: '{:,.0f}'.format(v).replace(',', '.')
ok = True
for ref, label, name, old, new, source in FIX:
    partner = P.search([('ref', '=', ref)])
    assert len(partner) == 1 and label in partner.name, ref
    row = Mov.search([('partner_id', '=', partner.id), ('comprobante', '=', name)])
    assert len(row) == 1 and row.tipo == 'pago' and abs(row.haber - old) < 0.01, name
    before = partner._distri_account_balance()
    row.write({'haber': new, 'concepto': '%s · importe corregido en el lab el 31/10/2026: en Online figura $%s; %s'
               % (row.concepto or '', money(old), source)})
    mirror = env['distri.online.payment'].sudo().search([('name', '=', name)])
    if len(mirror) == 1 and abs(mirror.amount - old) < 0.01:
        mirror.write({'amount': new})
    partner.message_post(body='Pago %s del %s corregido en la historia: $%s → $%s (%s). Hallado al cruzar las '
                              'planillas de efectivo.' % (name, row.fecha.strftime('%d/%m/%Y'), money(old),
                                                          money(new), source))
    env['distri.balance.review'].sudo()._distri_refresh(partner)
    env.flush_all()
    after = partner._distri_account_balance()
    ok = ok and abs((after - before) - (old - new)) < 0.01
    print('RESULT', partner.name, name, money(old), '->', money(new), 'espejo', len(mirror),
          'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
