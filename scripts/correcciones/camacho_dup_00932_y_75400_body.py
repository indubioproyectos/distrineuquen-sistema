# CAMACHO MARIA DE LOS ANGELES - VERDULERIA (ODOO-437), ale 30/10/2026:
# - PBNK1/2026/00932 (14/04, $117.750) was a duplicate of PBNK1/2026/00911
#   (one MP transfer, operation 153986969259). ale cancelled it in Online;
#   the lab's copy of the history row is archived the same way.
# - The second $75.400 transfer of 20/02 (card 45946, operation
#   146406013251), never loaded: Simon re-sent its receipt to the group on
#   15/04 19:40 «Maria Camacho». Confirmed with that receipt.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
partner = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-437')])
assert len(partner) == 1 and partner.name.startswith('CAMACHO MARIA')
Mov = uenv['distri.cuenta.movimiento'].sudo()
row = Mov.search([('partner_id', '=', partner.id), ('comprobante', '=', 'PBNK1/2026/00932')])
assert len(row) == 1 and abs(row.haber - 117750) < 0.01
card = uenv['distri.mp.collection'].sudo().browse(45946)
assert card.state == 'draft' and card.partner_id == partner and card.amount == 75400 and card.source_id == '146406013251'
assert card.receipt_id.id == 11168
before = partner._distri_account_balance()
row.write({'active': False,
           'concepto': '%s · anulado en Online el 30/10/2026: duplicado de PBNK1/2026/00911' % (row.concepto or '')})
card.with_context(distri_skip_confirm_notice=True).action_confirm()
partner.message_post(body='PBNK1/2026/00932 ($ 117.750, 14/04) anulado en Online por duplicado de PBNK1/2026/00911; '
                          'se archivó en la historia. Se confirmó la transferencia de $ 75.400 del 20/02 '
                          '(operación 146406013251), con el comprobante que Simon reenvió el 15/04 (30/10/2026).')
uenv['distri.balance.review'].sudo()._distri_refresh(partner)
env.flush_all()
after = partner._distri_account_balance()
print('RESULT', card.state, card.payment_id.name, 'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and card.state == 'confirmed' and abs(after - (before + 117750 - 75400)) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
