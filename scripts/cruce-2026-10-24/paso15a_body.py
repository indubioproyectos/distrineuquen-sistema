# Step 15a of the 24/10/2026 cross (odoo shell body, run by
# paso15a-grupo-a-ya-en-online.sh).
#
# Group A revision (26/10/2026): 44 pending Mercado Pago income cards whose
# money is already registered in Online. Most of the Online receipts are
# historical rows that are not in distri.online.payment, so the module's
# _distri_mark_online(pay) cannot be used. This replicates it without
# online_payment_id:
# - card: state 'ignored', ignore_reason 'online', ignore_note
#   «Ya cargado en Online: <comprobante(s)> (revisión grupo A 26/10)»;
# - its WhatsApp receipt, while pending / no_money / matched, is closed as
#   'registered' (_distri_close) with the same note;
# - a chatter message on the card.
# 38 cards are cat 'ya_en_online_libre' of the group A cross (46253 and 45946
# went to manual review and are left out); 6 more are covered by a sum of
# Online lines (40771, 40738, 46594 + 46593, 46362 + 46361).
# When the cross left several free Online receipts for a card, all of them
# are named in the note.
# Guards (ABORT, nothing written): a card missing, not an income card, not
# pending, or with another customer or amount; a payment created; a customer
# balance moved; any other income card changing state.
# Idempotent: a card already ignored as 'online' is skipped.
# Rolls back unless DO_COMMIT=1.
import json
import os
from collections import Counter

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
REVIEW = '(revisión grupo A 26/10)'

# (card id, customer ref, amount, Online receipt(s))
CARDS = [
    (40777, 'ODOO-38', 51570.00, 'PCSH1/2025/00002'),
    (40771, 'ODOO-33', 83500.00, 'BNK1/2025/00022 (78.900 + 4.600)'),
    (40738, 'ODOO-29', 130000.00, 'BNK1/2025/00039 (97.800 + 32.200)'),
    (41270, 'ODOO-31', 50000.00, 'BNK1/2025/00219'),
    (41246, 'ODOO-15', 100000.00, 'BNK1/2025/00470'),
    (41790, 'ODOO-31', 100000.00, 'BNK1/2025/00448, BNK1/2025/00407'),
    (41788, 'ODOO-31', 40000.00, 'BNK1/2025/00410'),
    (41752, 'ODOO-31', 100000.00, 'BNK1/2025/00448, BNK1/2025/00407'),
    (42107, 'ODOO-31', 100000.00, 'BNK1/2025/00659'),
    (42836, 'ODOO-36', 150000.00, 'BNK1/2025/00795'),
    (43056, 'ODOO-90', 700000.00, 'BNK1/2025/01273'),
    (43036, 'ODOO-196', 100000.00, 'BNK1/2025/01215, BNK1/2025/01286, BNK1/2025/01285, BNK1/2025/01292, BNK1/2025/01362'),
    (43686, 'ODOO-33', 100000.00, 'CSH1/2025/01279'),
    (43674, 'ODOO-196', 100000.00, 'BNK1/2025/01589, BNK1/2025/01595, BNK1/2025/01620, BNK1/2025/01680, BNK1/2025/01678'),
    (43670, 'ODOO-196', 100000.00, 'BNK1/2025/01589, BNK1/2025/01595, BNK1/2025/01620, BNK1/2025/01680, BNK1/2025/01678'),
    (43641, 'ODOO-196', 100000.00, 'BNK1/2025/01589, BNK1/2025/01595, BNK1/2025/01620, BNK1/2025/01680, BNK1/2025/01678'),
    (44607, 'ODOO-196', 100000.00, 'BNK1/2025/01589, BNK1/2025/01595, BNK1/2025/01620, BNK1/2025/01680, BNK1/2025/01678'),
    (44589, 'ODOO-352', 50000.00, 'BNK1/2025/01695'),
    (44311, 'ODOO-352', 90000.00, 'BNK1/2025/01874'),
    (44148, 'ODOO-208', 100000.00, 'BNK1/2025/01996'),
    (45233, 'ODOO-196', 100000.00, 'BNK1/2025/02033, BNK1/2025/02027, BNK1/2025/02044'),
    (45227, 'ODOO-196', 100000.00, 'BNK1/2025/02033, BNK1/2025/02027, BNK1/2025/02044'),
    (44959, 'ODOO-150', 30000.00, 'BNK1/2025/02191, BNK1/2025/02213'),
    (44906, 'ODOO-150', 30000.00, 'BNK1/2025/02191, BNK1/2025/02213'),
    (44829, 'ODOO-92', 450000.00, 'BNK1/2025/02280'),
    (46200, 'ODOO-92', 1000000.00, 'BNK1/2026/00417, BNK1/2026/00514'),
    (46163, 'ODOO-129', 100000.00, 'BNK1/2026/00446, BNK1/2026/00467'),
    (46129, 'ODOO-129', 100000.00, 'BNK1/2026/00446, BNK1/2026/00467, BNK1/2026/00540'),
    (46103, 'ODOO-21', 500000.00, 'BNK1/2026/00488'),
    (46061, 'ODOO-92', 1000000.00, 'BNK1/2026/00514'),
    (46024, 'ODOO-250', 100000.00, 'PBNK1/2026/00152'),
    (46017, 'ODOO-129', 100000.00, 'BNK1/2026/00467, BNK1/2026/00540'),
    (45850, 'ODOO-250', 100000.00, 'PBNK1/2026/00152'),
    (46594, 'ODOO-338', 94950.00, 'PBNK1/2026/00242 (94.950 + 28.000)'),
    (46593, 'ODOO-338', 28000.00, 'PBNK1/2026/00242 (94.950 + 28.000)'),
    (46589, 'ODOO-21', 400000.00, 'PBNK1/2026/00294'),
    (46435, 'ODOO-352', 50000.00, 'PCSH1/2026/00154, PBNK1/2026/00365, PCSH1/2026/00293, PBNK1/2026/00464'),
    (46362, 'ODOO-165', 84400.00, 'PBNK1/2026/00413 (84.400 + 10.500)'),
    (46361, 'ODOO-165', 10500.00, 'PBNK1/2026/00413 (84.400 + 10.500)'),
    (21803, 'ODOO-352', 50000.00, 'PBNK1/2026/00365, PCSH1/2026/00293, PBNK1/2026/00464'),
    (22557, 'ODOO-36', 250000.00, 'PBNK1/2026/00700'),
    (22745, 'ODOO-36', 100000.00, 'PBNK1/2026/01567'),
    (23454, 'ODOO-36', 100000.00, 'PBNK1/2026/02079, PBNK1/2026/02183'),
    (26887, 'ODOO-283', 80000.00, 'PBNK1/2026/03488'),
]
EXPECTED_COUNT = 44
EXPECTED_TOTAL = 7402920.00
OPEN_RECEIPT = ('pending', 'no_money', 'matched')

Payment = env['account.payment'].sudo()
Collection = env['distri.mp.collection'].sudo()


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


def log(**data):
    print('LOG', json.dumps(dict(paso='15a', **data), ensure_ascii=False, default=str))


def money(amount):
    text = '{:,.2f}'.format(amount).replace(',', 'X').replace('.', ',').replace('X', '.')
    return '$ ' + (text[:-3] if text.endswith(',00') else text)


# ------------------------------------------------------------ the list itself
ids = [row[0] for row in CARDS]
if len(CARDS) != EXPECTED_COUNT or len(set(ids)) != EXPECTED_COUNT:
    abort('la lista tiene %s filas (%s distintas), se esperaban %s'
          % (len(CARDS), len(set(ids)), EXPECTED_COUNT))
if abs(sum(row[2] for row in CARDS) - EXPECTED_TOTAL) > 0.01:
    abort('la lista suma %s, se esperaba %s' % (sum(row[2] for row in CARDS), EXPECTED_TOTAL))
if {46253, 45946} & set(ids):
    abort('46253 y 45946 van a revisión manual, no a este paso')

# ------------------------------------------------------------ pre-checks
cards = {c.id: c for c in Collection.browse(ids).exists()}
missing = [i for i in ids if i not in cards]
if missing:
    abort('no existen las tarjetas %s' % missing)
plan = []
stats = Counter()
for card_id, ref, amount, receipts in CARDS:
    card = cards[card_id]
    if card.kind != 'income':
        abort('la tarjeta %s no es un ingreso (%s)' % (card_id, card.kind))
    if card.partner_id.commercial_partner_id.ref != ref:
        abort('la tarjeta %s es de %s, se esperaba %s'
              % (card_id, card.partner_id.commercial_partner_id.ref, ref))
    if abs(card.amount - amount) > 0.01:
        abort('la tarjeta %s es por %s, se esperaba %s' % (card_id, card.amount, amount))
    note = 'Ya cargado en Online: %s %s' % (receipts, REVIEW)
    if card.state == 'ignored' and card.ignore_reason == 'online':
        stats['ya_cerrada'] += 1   # idempotent re-run
        continue
    if card.state != 'draft':
        abort('la tarjeta %s no está pendiente (%s / %s)' % (card_id, card.state, card.ignore_reason))
    plan.append((card, note))

others = Collection.search([('kind', '=', 'income'), ('id', 'not in', ids)])
states_before = {c.id: c.state for c in others}
payments_before = Payment.search_count([])
partners = Collection.browse(ids).mapped('partner_id.commercial_partner_id')
balances_before = {p.id: p._distri_account_balance() for p in partners}

# ------------------------------------------------------------ close
for card, note in plan:
    card.write({'state': 'ignored', 'ignore_reason': 'online', 'ignore_note': note})
    receipt = card.receipt_id
    receipt_before = receipt.distri_receipt_state if receipt else None
    if receipt and receipt.distri_receipt_state in OPEN_RECEIPT:
        receipt._distri_close('registered', note)
        stats['comprobante_cerrado'] += 1
    elif receipt:
        stats['comprobante_ya_cerrado:%s' % receipt.distri_receipt_state] += 1
    else:
        stats['sin_comprobante'] += 1
    card.message_post(body='%s. %s' % (note, money(card.amount)))
    stats['cerrada'] += 1
    log(accion='cerrada', tarjeta=card.id, op=card.source_id, cliente=card.partner_id.commercial_partner_id.ref,
        importe=card.amount, comprobante=receipt.id if receipt else None, comprobante_antes=receipt_before,
        nota=note)

# ------------------------------------------------------------ guards
env.flush_all()
if Payment.search_count([]) != payments_before:
    abort('se crearon pagos')
for partner in partners:
    if abs(partner._distri_account_balance() - balances_before[partner.id]) > 0.01:
        abort('cambió el saldo del cliente %s' % partner.ref)
for card in others:
    if card.state != states_before[card.id]:
        abort('la tarjeta %s, fuera de la lista, cambió de estado (%s -> %s)'
              % (card.id, states_before[card.id], card.state))
for card_id, _ref, _amount, _receipts in CARDS:
    card = cards[card_id]
    if card.state != 'ignored' or card.ignore_reason != 'online':
        abort('la tarjeta %s no quedó como ya cargada en Online (%s / %s)'
              % (card_id, card.state, card.ignore_reason))
    if card.receipt_id and card.receipt_id.distri_receipt_state in OPEN_RECEIPT:
        abort('el comprobante %s de la tarjeta %s quedó abierto (%s)'
              % (card.receipt_id.id, card_id, card.receipt_id.distri_receipt_state))

closed = [c for c, _n in plan]
print('RESULT', json.dumps({**dict(stats),
                            'tarjetas_lista': len(CARDS),
                            'importe_lista': EXPECTED_TOTAL,
                            'importe_cerrado_ahora': round(sum(c.amount for c in closed), 2),
                            'tablero': dict(Counter(Collection.search([('kind', '=', 'income')]).mapped(
                                lambda c: '%s:%s' % (c.state, c.ignore_reason or '-'))))},
                           ensure_ascii=False))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
