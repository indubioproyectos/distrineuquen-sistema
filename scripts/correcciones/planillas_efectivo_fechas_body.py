# Cash-sheet cross (planillas de efectivo 2026), ale 31/10/2026: three Online
# cash payments carry the wrong date (amounts are right), found when the sheet
# collections were crossed against Online. PCSH1/2026/02129 (KIOSCO MECHA,
# $688.350, Jorge) says 10/01 but it is the Plottier sheet of 01/10 (invoice
# 0001/2026/06391; day and month swapped, created in Online on 07/10).
# PCSH1/2026/01352 (JOHANA RAMIREZ, $72.400) and PCSH1/2026/01353 (ABELLO
# MICHELE, $74.100) say 31/03 but they are Simón's sheet of 31/07 (invoices
# 04548 and 04476; created in Online on 03/08). The lab's copy of the history
# gets the right dates; the administrator fixes Online.
# Rolls back unless DO_COMMIT=1.
import os
from datetime import date

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
Mov = env['distri.cuenta.movimiento'].sudo()
Partner = env['res.partner'].sudo()
FIX = [('ODOO-779', 'KIOSCO MECHA', 'PCSH1/2026/02129', date(2026, 1, 10), date(2026, 10, 1), 688350.0,
        'planilla de Jorge (Plottier) del 01/10, factura 0001/2026/06391'),
       ('ODOO-581', 'JOHANA RAMIREZ', 'PCSH1/2026/01352', date(2026, 3, 31), date(2026, 7, 31), 72400.0,
        'planilla de Simón del 31/07, factura 0001/2026/04548'),
       ('ODOO-289', 'ABELLO MICHELE', 'PCSH1/2026/01353', date(2026, 3, 31), date(2026, 7, 31), 74100.0,
        'planilla de Simón del 31/07, factura 0001/2026/04476')]
ok = True
for ref, label, name, old, new, amount, source in FIX:
    partner = Partner.search([('ref', '=', ref)])
    assert len(partner) == 1 and label in partner.name, ref
    before = partner._distri_account_balance()
    row = Mov.search([('partner_id', '=', partner.id), ('comprobante', '=', name)])
    assert len(row) == 1 and row.fecha == old and abs(row.haber - amount) < 0.01, name
    row.write({'fecha': new, 'concepto': '%s · fecha corregida en el lab el 31/10/2026 (en Online figura %s; %s)'
               % (row.concepto or '', old.strftime('%d/%m/%Y'), source)})
    mirror = env['distri.online.payment'].sudo().search([('name', '=', name)])
    if len(mirror) == 1:
        mirror.write({'date': new})
    partner.message_post(body='Pago %s ($%s) con fecha corregida en la historia: %s → %s (%s). '
                              'Hallado al cruzar las planillas de efectivo.'
                              % (name, '{:,.0f}'.format(amount).replace(',', '.'), old.strftime('%d/%m/%Y'),
                                 new.strftime('%d/%m/%Y'), source))
    env['distri.balance.review'].sudo()._distri_refresh(partner)
    env.flush_all()
    after = partner._distri_account_balance()
    ok = ok and abs(after - before) < 0.01
    print('RESULT', label, name, old, '->', new, 'espejo', len(mirror), 'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
