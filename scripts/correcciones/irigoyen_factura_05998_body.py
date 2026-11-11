# Invoice 0001/2026/05998 (17/09/2026, $154.800) was billed to MANGANARO
# VALENTIN - MARIA PAZ - AUTOSERVICIO GENOVES (ODOO-281) but belongs to
# ALMACEN MARKET IRIGOYEN - VALENTIN (ODOO-119): Irigoyen buys once a month
# and pays cash the same day, and the 21/09 notice «Market irigoyen - Valentin
# $ 154.800 efectivo» (PBNK1/2026/03849) was its only September movement;
# Manganaro always pays by transfer. ale (30/10/2026): move it in the lab's
# copy of the history; the administrator moves it in Online.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
P = env['res.partner'].sudo()
irigoyen = P.search([('ref', '=', 'ODOO-119')])
manganaro = P.search([('ref', '=', 'ODOO-281')])
assert len(irigoyen) == 1 and len(manganaro) == 1
row = env['distri.cuenta.movimiento'].sudo().search([('partner_id', '=', manganaro.id), ('comprobante', '=', '0001/2026/05998')])
assert len(row) == 1 and row.tipo == 'factura' and abs(row.debe - 154800) < 0.01
b_i, b_m = irigoyen._distri_account_balance(), manganaro._distri_account_balance()
row.write({'partner_id': irigoyen.id,
           'concepto': '%s · pasada de ODOO-281 (Manganaro) a ODOO-119 en el lab el 30/10/2026' % (row.concepto or '')})
irigoyen.message_post(body='Se pasó a esta cuenta la factura 0001/2026/05998 (17/09, $ 154.800), cargada en Online a '
                           'MANGANARO VALENTIN; la pagó en efectivo el 21/09 (PBNK1/2026/03849) (30/10/2026).')
manganaro.message_post(body='La factura 0001/2026/05998 (17/09, $ 154.800) era de ALMACEN MARKET IRIGOYEN - VALENTIN; '
                            'se pasó a esa cuenta en el lab (30/10/2026). Falta cambiarla en Online.')
env['distri.balance.review'].sudo()._distri_refresh(irigoyen | manganaro)
env.flush_all()
a_i, a_m = irigoyen._distri_account_balance(), manganaro._distri_account_balance()
print('RESULT irigoyen %.2f -> %.2f | manganaro %.2f -> %.2f' % (b_i, a_i, b_m, a_m))
if DO_COMMIT and abs(a_i - b_i - 154800) < 0.01 and abs(b_m - a_m - 154800) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
