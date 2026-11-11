# DESPENSA BM - MARTINEZ MERCEDES BELEN - CUENCA XV (ODOO-476), ale 30/10/2026:
# 1. PBNK1/2026/02416 (04/06, $101.680) came from her account
#    «BebidasyMilasBM» and completes her invoice 03274 ($260.000 cash +
#    $101.680), but Online has it on Y - MARIANA BOGADO (ODOO-220): the lab's
#    history row moves to Despensa BM.
# 2. PCSH1/2026/01060 (16/06, $200.000 cash, loaded on 29/06) repeats
#    PCSH1/2026/00991: invoice 03488 ($505.980) = one $200.000 cash + the
#    $305.980 transfer. The repeated row is archived.
# 3. The $446.800 transfer of 09/07 from her account (card 24938), exact
#    amount of invoice 03992 of the same day, no receipt in the group:
#    approved without receipt (the receipt is asked for in the message).
# Online is fixed by the administrator. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
P = uenv['res.partner'].sudo()
bm, bogado = P.search([('ref', '=', 'ODOO-476')]), P.search([('ref', '=', 'ODOO-220')])
assert len(bm) == 1 and len(bogado) == 1
Mov = uenv['distri.cuenta.movimiento'].sudo()
moved = Mov.search([('partner_id', '=', bogado.id), ('comprobante', '=', 'PBNK1/2026/02416')])
assert len(moved) == 1 and abs(moved.haber - 101680) < 0.01
dup = Mov.search([('partner_id', '=', bm.id), ('comprobante', '=', 'PCSH1/2026/01060')])
assert len(dup) == 1 and abs(dup.haber - 200000) < 0.01
card = uenv['distri.mp.collection'].sudo().browse(24938)
assert card.state == 'draft' and card.partner_id == bm and abs(card.amount - 446800) < 0.01 and not card.receipt_id
b_bm, b_bo = bm._distri_account_balance(), bogado._distri_account_balance()
moved.write({'partner_id': bm.id, 'concepto': '%s · pasado de ODOO-220 (Bogado) a ODOO-476 en el lab el 30/10/2026'
             % (moved.concepto or '')})
mirror = uenv['distri.online.payment'].sudo().search([('name', '=', 'PBNK1/2026/02416')])
if len(mirror) == 1 and 'partner_id' in mirror._fields:
    mirror.write({'partner_id': bm.id})
dup.write({'active': False, 'concepto': '%s · repetido de PCSH1/2026/00991; archivado en el lab el 30/10/2026, falta '
                                        'anularlo en Online' % (dup.concepto or '')})
if card.receipt_candidate:
    uenv['distri.mp.vouch'].sudo().create({'collection_id': card.id, 'note':
        'Lo confirmó ale el 30/10/2026: transferencia desde su cuenta por el importe exacto de la factura 03992; '
        'se le pide el comprobante'}).action_apply()
else:
    card.with_context(distri_skip_confirm_notice=True).action_confirm()
    card.message_post(body='Confirmado sin comprobante: lo confirmó ale el 30/10/2026 (importe exacto de la factura '
                           '03992 del mismo día, desde su cuenta); se le pide el comprobante.')
bm.message_post(body='30/10/2026: se pasó desde ODOO-220 la transferencia PBNK1/2026/02416 ($ 101.680, 04/06), se '
                     'archivó el efectivo repetido PCSH1/2026/01060 ($ 200.000, 16/06) y se aprobó sin comprobante la '
                     'transferencia de $ 446.800 del 09/07.')
bogado.message_post(body='PBNK1/2026/02416 ($ 101.680, 04/06) era de DESPENSA BM; se pasó a esa cuenta en el lab '
                         '(30/10/2026). Falta cambiarla en Online.')
uenv['distri.balance.review'].sudo()._distri_refresh(bm | bogado)
env.flush_all()
a_bm, a_bo = bm._distri_account_balance(), bogado._distri_account_balance()
print('RESULT', card.state, card.payment_id.name, '| BM %.2f -> %.2f | Bogado %.2f -> %.2f' % (b_bm, a_bm, b_bo, a_bo))
ok = abs(a_bm - (b_bm - 101680 + 200000 - 446800)) < 0.01 and abs(a_bo - b_bo - 101680) < 0.01
if DO_COMMIT and ok and card.state == 'confirmed':
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED', 'ok' if ok else 'NO CIERRA')
