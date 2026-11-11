# LAIME SAMUEL - KIOSCO SAMU (ODOO-133): three Mercado Pago transfers from
# his own account «Kiosco» (CUIT 23-35597502-9, used only by him in 2026)
# with no receipt in the WhatsApp group: 25/08 $399.020 and 27/10 $268.950
# (the exact invoice of the same day; the 27/10 cash sheet of Simon has a dash)
# and 08/09 $416.580 (invoice 05652 of $445.180). Approved without receipt on
# ale's word (29/10/2026), the board's own way («Aprobar sin comprobante» for
# the candidates, «Confirmar» for the other one). Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CUIT = '23355975029'
CARDS = {25417: 399020.0, 26787: 416580.0, 50382: 268950.0}
NOTE = 'Lo confirmó ale el 29/10/2026: transferencia desde su cuenta «Kiosco», que usa solo este cliente'
uenv = env(user=env.ref('base.user_admin').id)
partner = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-133')])
assert len(partner) == 1 and partner.name.startswith('LAIME SAMUEL'), partner.mapped('name')
Card = uenv['distri.mp.collection'].sudo()
others = Card.search([('payer_cuit', '=', CUIT), ('kind', '=', 'income'), ('date', '>=', '2026-01-01'),
                      ('partner_id', 'not in', [False, partner.id])])
assert not others, others.mapped('partner_id.name')
cards = Card.browse(list(CARDS))
for card in cards:
    assert card.kind == 'income' and card.state == 'draft' and card.payer_cuit == CUIT, card.name
    assert abs(card.amount - CARDS[card.id]) < 0.01, (card.id, card.amount)
    assert card.partner_id == partner, (card.id, card.partner_id.name)
before = partner._distri_account_balance()
for card in cards:
    if card.receipt_candidate and not card.receipt_id:
        wizard = uenv['distri.mp.vouch'].sudo().create({'collection_id': card.id, 'note': NOTE})
        wizard.action_apply()
    else:
        card.with_context(distri_skip_confirm_notice=True).action_confirm()
        card.message_post(body='Confirmado sin comprobante: %s.' % NOTE)
env.flush_all()
after = partner._distri_account_balance()
total = sum(CARDS.values())
print('RESULT estados=%s pagos=%s saldo %.2f -> %.2f (baja %.2f)' % (
    cards.mapped('state'), cards.mapped('payment_id.name'), before, after, before - after))
if DO_COMMIT and set(cards.mapped('state')) == {'confirmed'} and abs(before - after - total) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
