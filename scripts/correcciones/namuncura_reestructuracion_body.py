# JOSE RUBALDO CARO NAMUNCURA (ODOO-89), ale 30/10/2026:
# - The $200.000 MP transfer of 19/07 (card 24652, PMP/2026/00038) was never
#   loaded in Online; on 10/08 Online got a «REESTRUCTURACION» of $268.943
#   (PBNK1/2026/02878), exactly Online's balance then ($68.943 + those
#   $200.000), which left the account at $0. Counting both credits him twice:
#   PMP/2026/00038 is undone and the card goes back to «Revisar» with a
#   conflict note, to see with the administrator.
# - The $74.000 transfer of 23/10 from Sandra Feldano (card 27170, same payer
#   as the 25/10 $229.400 Denis assigned) pays invoice 07131 ($86.400 minus
#   $12.500 cash): approved.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
C = uenv['distri.mp.collection']
partner = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-89')])
assert len(partner) == 1 and 'NAMUNCURA' in partner.name
double, feldano = C.browse(24652), C.browse(27170)
assert double.state == 'confirmed' and double.payment_id.name == 'PMP/2026/00038' and double.amount == 200000
assert feldano.state == 'draft' and feldano.partner_id == partner and feldano.amount == 74000
before = partner._distri_account_balance()
double.action_undo()
double.sudo().write({'partner_id': partner.id, 'conflict_note':
    'A revisar con Administración: no está en Online y la «REESTRUCTURACION» del 10/08 (PBNK1/2026/02878, '
    '$268.943) ya la incluye en el saldo que dejó en $0. No se confirma hasta verlo (30/10/2026).'})
double.sudo().message_post(body='Vuelve al tablero sin confirmar (era PMP/2026/00038): la reestructuración del 10/08 '
                                'ya la contó (30/10/2026).')
if feldano.receipt_candidate and not feldano.receipt_id:
    uenv['distri.mp.vouch'].sudo().create({'collection_id': feldano.id, 'note':
        'Lo confirmó ale el 30/10/2026: misma cuenta que el pago del 25/10; paga la factura 07131'}).action_apply()
else:
    feldano.with_context(distri_skip_confirm_notice=True).action_confirm()
    feldano.message_post(body='Confirmado: lo confirmó ale el 30/10/2026 (misma cuenta que el pago del 25/10; '
                              'paga la factura 07131).')
(double | feldano).sudo()._migration_recompute()
uenv['distri.balance.review'].sudo()._distri_refresh(partner)
env.flush_all()
after = partner._distri_account_balance()
print('RESULT doble', double.state, double.column, '| 74000', feldano.state, feldano.payment_id.name,
      '| saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and double.state == 'draft' and double.column == 'review' and feldano.state == 'confirmed' \
        and abs(after - before - 126000) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
