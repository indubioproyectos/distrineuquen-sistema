# MENDEZ ORLANDO ANTONIO - MERCADO SALMA (ODOO-40), ale 30/10/2026: invoice
# 0001/2026/04655 (06/08, $324.520) was paid twice: on 07/08 by MP (card
# 26000, PMP/2026/00044, only in the lab; Online still shows it unpaid) and
# again inside the $495.770 of 04/10 (exactly the Online balance then). The
# 07/08 payment is undone and goes back to the board unconfirmed, with a
# conflict note in «Revisar» (never confirmed by «Confirmar todos»), to see with the administrator first.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
partner = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-40')])
assert len(partner) == 1 and partner.name.startswith('MENDEZ ORLANDO')
card = uenv['distri.mp.collection'].browse(26000)
assert card.state == 'confirmed' and card.payment_id.name == 'PMP/2026/00044' and abs(card.amount - 324520) < 0.01
before = partner._distri_account_balance()
card.action_undo()
assert card.state == 'draft', card.state
card.sudo().write({'partner_id': partner.id,
                   'conflict_note': 'A revisar con Administración: pago de la factura 04655 del 06/08 que no está en '
                                    'Online; el cliente la volvió a pagar dentro de los $495.770 del 04/10. No se '
                                    'confirma hasta verlo (30/10/2026).'})
card.sudo().message_post(body='Vuelve al tablero sin confirmar (era PMP/2026/00044): doble pago de la factura 04655, '
                              'se ve primero con el administrador (30/10/2026).')
card.sudo()._migration_recompute()
uenv['distri.balance.review'].sudo()._distri_refresh(partner)
env.flush_all()
after = partner._distri_account_balance()
print('RESULT', card.state, card.column, card.migration_box, 'saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and card.state == 'draft' and card.column == 'review' and abs(after - before - 324520) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
