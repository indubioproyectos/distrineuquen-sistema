# Mercado Pago transfers from the customer's own account (a CUIT that in 2026
# paid only for that customer) with no receipt in the WhatsApp group, each one
# the exact amount of an invoice. Approved without receipt on ale's word
# (29/10/2026), the board's own way («Aprobar sin comprobante» for the
# candidates, «Confirmar» for the rest). Input: env JOBS = JSON list of
# {"ref": "ODOO-…", "cuit": "…", "cards": {"<id>": <amount>}}.
# Rolls back unless DO_COMMIT=1.
import json
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
JOBS = json.loads(os.environ['JOBS'])
NOTE = 'Lo confirmó ale el 29/10/2026: transferencia desde la cuenta propia del cliente, por el importe exacto de su factura'
uenv = env(user=env.ref('base.user_admin').id)
Card = uenv['distri.mp.collection'].sudo()
ok = True
for job in JOBS:
    partner = uenv['res.partner'].sudo().search([('ref', '=', job['ref'])])
    assert len(partner) == 1, job['ref']
    others = Card.search([('payer_cuit', '=', job['cuit']), ('kind', '=', 'income'), ('date', '>=', '2026-01-01'),
                          ('partner_id', 'not in', [False, partner.id])])
    assert not others, (job['ref'], others.mapped('partner_id.name'))
    cards = Card.browse([int(i) for i in job['cards']])
    for card in cards:
        assert card.kind == 'income' and card.state == 'draft' and card.payer_cuit == job['cuit'], card.id
        assert abs(card.amount - job['cards'][str(card.id)]) < 0.01, (card.id, card.amount)
        assert card.partner_id == partner, (card.id, card.partner_id.name)
    before = partner._distri_account_balance()
    for card in cards:
        if card.receipt_candidate and not card.receipt_id:
            uenv['distri.mp.vouch'].sudo().create({'collection_id': card.id, 'note': NOTE}).action_apply()
        else:
            card.with_context(distri_skip_confirm_notice=True).action_confirm()
            card.message_post(body='Confirmado sin comprobante: %s.' % NOTE)
    env.flush_all()
    after = partner._distri_account_balance()
    total = sum(job['cards'].values())
    good = set(cards.mapped('state')) == {'confirmed'} and abs(before - after - total) < 0.01
    ok = ok and good
    print('RESULT %s %s pagos=%s saldo %.2f -> %.2f' % (job['ref'], partner.name, cards.mapped('payment_id.name'),
                                                        before, after))
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
