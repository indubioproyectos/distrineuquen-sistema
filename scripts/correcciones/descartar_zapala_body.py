# Discard the pending Mercado Pago cards of Zapala customers on the payments
# board (odoo shell body, run by descartar-zapala.sh). ale verified every
# Zapala balance against the manager's figures on 27/10/2026, so confirming
# these cards would discount the same money twice. Same write as the
# «Descartar» wizard (distri.mp.ignore.action_ignore), reason «other».
# Prints IDS with the discarded card ids (for undo). Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
NOTE = 'Zapala: saldos verificados con el gerente (27/10/2026)'
C = env['distri.mp.collection'].sudo()
cards = C.search([('state', '=', 'draft'), '|', ('partner_id.name', '=like', 'Z -%'),
                  ('partner_id.name', 'ilike', 'zapala')])
for partner in cards.mapped('partner_id'):
    mine = cards.filtered(lambda c: c.partner_id == partner)
    print('CLIENTE %d %.2f %s' % (len(mine), sum(mine.mapped('amount')), partner.name))
for card in cards:
    card.write({'state': 'ignored', 'ignore_reason': 'other', 'ignore_note': NOTE})
    if card.kind in ('receipt', 'verify') and card.receipt_id:
        card.receipt_id.distri_receipt_state = 'dismissed'
    card.message_post(body='Descartado por %s: Otro motivo. %s' % (env.user.name, NOTE))
left = C.search_count([('id', 'in', cards.ids), ('state', '=', 'draft')])
print('RESULT tarjetas=%d importe=%.2f quedan_pendientes=%d' % (len(cards), sum(cards.mapped('amount')), left))
print('IDS', ','.join(map(str, cards.ids)))
if DO_COMMIT and not left:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
