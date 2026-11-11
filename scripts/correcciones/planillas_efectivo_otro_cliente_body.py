# Cash-sheet cross (planillas de efectivo 2026), ale 31/10/2026: three Online
# cash payments of Simón are on a customer with a similar name. Each one is on
# the sheet on its own row for the other customer, whose invoice of exactly
# that amount is still open, and the customer that has it in Online has no
# invoice it pays (checked again by a blind agent). The lab's copy of the
# history moves them (Online is fixed by the administrator):
#   PCSH1/2026/01640 24/08 $119.450  VALERIA EL CORSA (109) -> VALERIA ANDRES (349), invoice 05135
#   PCSH1/2026/01173 08/07 $81.600   DIAZ EMANUEL (483)     -> DIAZ HORACIO (339),   invoice 03975
#   PCSH1/2026/00859 18/05 $76.300   DAIANA JELDRES (555)   -> DAIANA ARONOFF (593), invoice 02850
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
P = uenv['res.partner'].sudo()
Mov = uenv['distri.cuenta.movimiento'].sudo()
MOVES = [('PCSH1/2026/01640', 119450.0, 'ODOO-109', 'EL CORSA', 'ODOO-349', 'VALERIA ANDRES', '24/08', '05135'),
         ('PCSH1/2026/01173', 81600.0, 'ODOO-483', 'DIAZ EMANUEL', 'ODOO-339', 'DIAZ HORACIO', '08/07', '03975'),
         ('PCSH1/2026/00859', 76300.0, 'ODOO-555', 'JELDRES', 'ODOO-593', 'ARONOFF', '18/05', '02850')]
ok = True
for name, amount, ref_from, label_from, ref_to, label_to, day, invoice in MOVES:
    src, dst = P.search([('ref', '=', ref_from)]), P.search([('ref', '=', ref_to)])
    assert len(src) == 1 and label_from in src.name and len(dst) == 1 and label_to in dst.name, name
    row = Mov.search([('partner_id', '=', src.id), ('comprobante', '=', name)])
    assert len(row) == 1 and row.tipo == 'pago' and abs(row.haber - amount) < 0.01, name
    b_src, b_dst = src._distri_account_balance(), dst._distri_account_balance()
    row.write({'partner_id': dst.id,
               'concepto': '%s · pasado de %s a %s en el lab el 31/10/2026 (planilla de Simón del %s, factura '
                           '0001/2026/%s)' % (row.concepto or '', ref_from, ref_to, day, invoice)})
    mirror = uenv['distri.online.payment'].sudo().search([('name', '=', name)])
    if len(mirror) == 1:
        mirror.write({'partner_id': dst.id})
    text = ('El efectivo %s del %s ($ %s) se pasó de %s a %s: en la planilla de Simón figura en su propio renglón '
            'para %s, con la factura 0001/2026/%s del mismo importe (31/10/2026).')
    money = '{:,.0f}'.format(amount).replace(',', '.')
    for p in (src, dst):
        p.message_post(body=text % (name, day, money, src.name, dst.name, dst.name, invoice))
    uenv['distri.balance.review'].sudo()._distri_refresh(src | dst)
    env.flush_all()
    a_src, a_dst = src._distri_account_balance(), dst._distri_account_balance()
    ok = ok and abs(a_src - b_src - amount) < 0.01 and abs(b_dst - a_dst - amount) < 0.01
    print('RESULT', name, '| %s %.2f -> %.2f | %s %.2f -> %.2f' % (src.name, b_src, a_src, dst.name, b_dst, a_dst))
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
