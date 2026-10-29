# Step 15b of the 24/10/2026 cross (odoo shell body, run by
# paso15b-listas-para-confirmar.sh).
#
# Marks the 16 board cards that the group B review (4 agents + blind check,
# grupo_b/para_confirmar.csv) found safe to confirm: each pays a real invoice
# or leaves the customer still owing, never in credit. Nothing is confirmed
# here: Administration confirms each one by hand from the board.
# - a note in each card's chatter
# - a shared saved filter on the board, «Listas para confirmar (revisión
#   26/10)», limited to these cards while they stay pending
# Guards (ABORT, nothing written): a card missing, not an income, not pending,
# or with another customer or amount. Idempotent. Rolls back unless
# DO_COMMIT=1.
import json
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
NOTE = ('Lista para confirmar (revisión del 26/10/2026): paga una factura real o deja al '
        'cliente con deuda, nunca con saldo a favor. Confirmar a mano desde el tablero.')
FILTER_NAME = 'Listas para confirmar (revisión 26/10)'

# operation, customer ref, amount
CARDS = [
    ('174804185488', 'ODOO-13', 55000.00),
    ('184709318628', 'ODOO-196', 115000.00),
    ('184708453938', 'ODOO-196', 100000.00),
    ('180030053787', 'ODOO-212', 113000.00),
    ('116261800535', 'ODOO-32', 40000.00),
    ('131936583899', 'ODOO-32', 20650.00),
    ('156854718039', 'ODOO-360', 78600.00),
    ('156853682157', 'ODOO-360', 280000.00),
    ('148451345347', 'ODOO-388', 106500.00),
    ('161873349597', 'ODOO-388', 97400.00),
    ('179855967055', 'ODOO-397', 56600.00),
    ('182898343969', 'ODOO-397', 24900.00),
    ('183920682612', 'ODOO-397', 27750.00),
    ('177125232825', 'ODOO-551', 179900.00),
    ('177078018150', 'ODOO-694', 74700.00),
    ('150911188772', 'ODOO-289', 150000.00),
]


def abort(msg):
    env.cr.rollback()
    print('ABORT', msg)
    print('NOT COMMITTED (aborted)')
    raise SystemExit(0)


assert len({c[0] for c in CARDS}) == 16
Collection = env['distri.mp.collection'].sudo()
cards = Collection.browse()
for op, ref, amount in CARDS:
    card = Collection.search([('kind', '=', 'income'), ('source_id', '=', op)])
    if len(card) != 1:
        abort('operación %s: %s tarjetas' % (op, len(card)))
    if card.state != 'draft':
        abort('operación %s no está pendiente (%s)' % (op, card.state))
    if (card.partner_id.commercial_partner_id.ref or '') != ref:
        abort('operación %s: cliente %s, se esperaba %s' % (op, card.partner_id.ref, ref))
    if abs(card.amount - amount) > 0.01:
        abort('operación %s: importe %s, se esperaba %s' % (op, card.amount, amount))
    cards |= card

noted = 0
for card in cards:
    if not card.message_ids.filtered(lambda m: NOTE in (m.body or '')):
        card.message_post(body=NOTE)
        noted += 1

action = env.ref('distri_pedido.action_mp_board')
domain = repr([('id', 'in', cards.ids), ('state', '=', 'draft')])
Filter = env['ir.filters'].sudo()
existing = Filter.search([('name', '=', FILTER_NAME), ('model_id', '=', 'distri.mp.collection')])
values = {'name': FILTER_NAME, 'model_id': 'distri.mp.collection', 'domain': domain,
          'action_id': action.id, 'user_ids': [(5, 0, 0)], 'context': '{}', 'sort': '[]'}
if existing:
    existing.write(values)
else:
    Filter.create(values)

print('RESULT', json.dumps({'tarjetas': len(cards), 'notas_nuevas': noted,
                            'importe': round(sum(cards.mapped('amount')), 2),
                            'filtro': 'actualizado' if existing else 'creado'}, ensure_ascii=False))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
