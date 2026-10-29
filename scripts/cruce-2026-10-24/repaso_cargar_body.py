# Step 10 of the 24/10/2026 cross (odoo shell body, run by paso10-repaso.sh).
#
# Loads the Mercado Pago transfers the re-audit of 25/10/2026 found unloaded
# (auditoria/repaso/informe.md), read from paso10_casos.csv:
# - borrador_online: the module closed the card as «Ya en Online» against an
#   Online payment that is still a draft (it counts in no balance), or left it
#   pending while Online has only a draft of the same day and amount;
# - credito_duplicado: the card was closed against an Online credit that is
#   the 10-second duplicate of another one; the transfer belongs to the
#   customer of its receipt;
# - factura_exacta: the amount is not round and equals exactly one invoice of
#   one customer in the 30 days before, with no other payment of that amount.
# A card closed as «Ya en Online» is first unlinked (online_unlinked: never
# linked by amount again); then the card is confirmed for the customer through
# the module (action_confirm), with a chatter note «Repaso del 25/10/2026: …»
# naming the evidence (once: the note is the idempotency mark).
# Skipped (SKIP, left for the review page): a customer that would end in
# credit after the loads, an Online credit of the same customer and amount
# from 5 days before to 60 days after the transfer, a payer CUIT of an
# employee.
# Guards (ABORT, nothing written): a card or customer not as expected, payment
# or card totals that do not move by exactly the loads, any customer balance
# that does not move by exactly its loads.
# Rolls back unless DO_COMMIT=1.
import csv
import json
import os
from collections import Counter, defaultdict
from datetime import date, timedelta

from odoo.addons.distri_pedido.models.reparto import _money

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IN = os.environ.get('IN_DIR', '/cruce')
MARK = 'Repaso del 25/10/2026'
EXPECTED = 25
WHY = {
    'borrador_online': 'en Online solo hay un pago en borrador de ese importe, que no suma al saldo',
    'credito_duplicado': 'el cobro de Online con el que estaba unido es el duplicado de otro del mismo cliente '
                         '(creado 10 segundos después, misma factura); el comprobante es de este cliente',
    'factura_exacta': 'el importe coincide exactamente con una sola factura del cliente de los 30 días '
                      'anteriores y no hay otro cobro de ese importe',
}

Partner = env['res.partner'].sudo().with_context(active_test=False)
Payment = env['account.payment'].sudo()
Collection = env['distri.mp.collection'].sudo()
Message = env['mail.message'].sudo()
Identity = env['distri.payer.identity'].sudo()


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


def log(**data):
    print('LOG', json.dumps(dict(paso=10, **data), ensure_ascii=False, default=str))


def skip(**data):
    print('SKIP', json.dumps(data, ensure_ascii=False, default=str))


def noted(record):
    return bool(Message.search_count([('model', '=', record._name), ('res_id', '=', record.id),
                                      ('body', 'ilike', MARK)]))


def digits(value):
    return ''.join(ch for ch in (value or '') if ch.isdigit())


def lab_partner(ref):
    found = Partner.search([('ref', '=', ref)])
    commercial = found.commercial_partner_id
    if len(commercial) != 1 or not commercial.active or commercial.distri_generic_partner:
        abort('cliente %s: no está, no es único, está archivado o es genérico' % ref)
    return commercial


def summary_sql(query):
    env.flush_all()
    env.cr.execute(query)
    return {row[0]: [row[1], round(float(row[2] or 0), 2)] for row in env.cr.fetchall()}


def payment_summary():
    return summary_sql('select state, count(*), sum(amount) from account_payment group by 1')


def card_summary():
    return summary_sql("select state, count(*), sum(amount) from distri_mp_collection "
                       "where kind = 'income' group by 1")


with open(os.path.join(IN, 'paso10_casos.csv'), encoding='utf-8') as handle:
    rows = list(csv.DictReader(handle))
if len(rows) != EXPECTED or len({r['op'] for r in rows}) != EXPECTED:
    abort('paso10_casos.csv no tiene las %s operaciones' % EXPECTED)
with open(os.path.join(IN, 'creditos.json'), encoding='utf-8') as handle:
    credits = json.load(handle)
employees = {digits(c) for c in json.load(open(os.path.join(IN, 'cuits_empleados.json'), encoding='utf-8'))}
credits_by_partner = defaultdict(list)
for line in credits:
    if line.get('partner_id'):
        credits_by_partner['ODOO-%s' % line['partner_id'][0]].append(line)

payments_before = payment_summary()
cards_before = card_summary()
all_partners = Partner.search([('ref', '=like', 'ODOO-%'), ('active', '=', True)]).filtered(
    lambda p: p == p.commercial_partner_id)
balances_before = {p.id: p._distri_account_balance() for p in all_partners}

# ------------------------------------------------------------------ check
todo, stats = [], Counter()
per_customer = defaultdict(float)
for row in rows:
    card = Collection.search([('kind', '=', 'income'), ('source_id', '=', row['op'])])
    if len(card) != 1 or abs(card.amount - float(row['importe'])) > 0.005:
        abort('la operación %s no está en el tablero con ese importe' % row['op'])
    target = lab_partner(row['cliente'])
    if card.state == 'confirmed' and card.partner_id.commercial_partner_id == target and noted(card):
        stats['ya_cargado'] += 1
        continue
    if card.payment_id or card.state == 'confirmed':
        abort('la operación %s ya tiene un pago' % row['op'])
    if row['tipo'] in ('borrador_online', 'credito_duplicado') and card.state == 'ignored':
        pay = card.online_payment_id
        if card.ignore_reason != 'online' or not pay or card.export_batch_id:
            abort('la operación %s no está cerrada como Ya en Online' % row['op'])
        if row['tipo'] == 'borrador_online' and pay.state != 'draft':
            abort('el pago de Online de la operación %s no está en borrador' % row['op'])
    elif card.state not in ('draft', 'apartado'):
        abort('la operación %s está %s' % (row['op'], card.state))
    if digits(card.payer_cuit) in employees:
        stats['skip_empleado'] += 1
        skip(op=row['op'], motivo='CUIT de un empleado')
        continue
    day = card.date.date() if hasattr(card.date, 'date') else card.date
    twin = [l for l in credits_by_partner[row['cliente']]
            if abs(l['credit'] - card.amount) <= 0.005
            and day - timedelta(days=5) <= date.fromisoformat(l['date']) <= day + timedelta(days=60)]
    if twin and row['tipo'] == 'factura_exacta':
        stats['skip_credito_igual'] += 1
        skip(op=row['op'], motivo='Online ya tiene un cobro igual del cliente',
             credito=[(l['id'], l['date']) for l in twin])
        continue
    todo.append((row, card, target))
    per_customer[target.id] += card.amount

# a customer that would end in credit is left for review, all its rows
in_credit = {pid for pid, total in per_customer.items() if balances_before.get(pid, 0.0) - total < -0.01}
for pid in in_credit:
    stats['skip_queda_a_favor'] += sum(1 for _r, _c, t in todo if t.id == pid)
    skip(cliente=Partner.browse(pid).ref, motivo='quedaría a favor', saldo=round(balances_before.get(pid, 0.0), 2),
         carga=round(per_customer[pid], 2))
todo = [(r, c, t) for r, c, t in todo if t.id not in in_credit]

# ------------------------------------------------------------------ load
identities_before = Identity.search([]).ids
expected_move = Counter()
card_delta = Counter()
created = Payment.browse()
for row, card, target in todo:
    was = card.state
    if was == 'ignored':
        pay = card.online_payment_id
        label = pay.name or (pay.move_name or '').split(' ')[0] or 'pago %s de Online' % pay.online_id
        pay.write({'collection_id': False, 'link_kind': False})
        card.write({'online_payment_id': False, 'online_unlinked': True, 'online_conflict': False,
                    'state': 'draft', 'ignore_reason': False, 'ignore_note': False})
        unlinked = ' Estaba unido por importe a %s (%s, %s).' % (label, pay.state, _money(pay.amount))
    else:
        unlinked = ''
        if was == 'apartado':
            card._distri_bring_back('repaso del 25/10/2026')
    card.write({'partner_id': target.id, 'proposal': 'manual',
                'proposal_note': ('%s: %s' % (MARK, row['tipo']))[:250]})
    card.action_confirm()
    new = card.payment_id
    if card.state != 'confirmed' or not new or new.partner_id.commercial_partner_id != target or \
            abs(new.amount - card.amount) > 0.005:
        abort('la operación %s no quedó confirmada para %s' % (row['op'], row['cliente']))
    if not noted(card):
        text = '%s: se carga a %s porque %s.%s Evidencia: %s' % (
            MARK, target.name, WHY[row['tipo']], unlinked, row['evidencia'])
        card.message_post(body=text)
        new.message_post(body=text)
    created |= new
    card_delta[(was, 'n')] -= 1
    card_delta[(was, 'amount')] -= card.amount
    card_delta[('confirmed', 'n')] += 1
    card_delta[('confirmed', 'amount')] += card.amount
    expected_move[target.id] -= new.amount
    stats['cargado:%s' % row['tipo']] += 1
    log(accion='cargado', op=row['op'], cobro=card.id, pago=new.id, nombre=new.name, cliente=row['cliente'],
        tipo=row['tipo'], estado_anterior=was, importe=new.amount)

# confirming «A mano» teaches the module the payer's CUIT, and a new identity
# re-proposes every pending card of that CUIT to this customer (blind check
# 25/10: 25 cards, $13.6 M, mostly not this customer's). These loads rest on
# one invoice each, not on the payer: the identities they created are removed
# and those cards proposed again as before.
new_identities = Identity.search([('id', 'not in', identities_before)])
if new_identities:
    cuits = new_identities.filtered(lambda i: i.kind == 'cuit').mapped('value')
    log(accion='identidades_quitadas', identidades=[(i.kind, i.value, i.partner_id.ref) for i in new_identities])
    new_identities.unlink()
    pending = Collection.search([('state', '=', 'draft'), ('kind', '=', 'income'), ('payer_cuit', 'in', cuits),
                                 ('proposal', '!=', 'manual')])
    pending._propose()
    if pending.filtered(lambda c: c.proposal == 'cuit'):
        abort('quedaron tarjetas propuestas por un CUIT quitado: %s' % pending.filtered(
            lambda c: c.proposal == 'cuit').ids)
    log(accion='tarjetas_repropuestas', n=len(pending),
        propuestas=dict(Counter(pending.mapped(lambda c: c.proposal or 'sin_propuesta'))))
    stats['identidades_quitadas'] = len(new_identities)

# ------------------------------------------------------------------ guards
env.flush_all()
payments_after = payment_summary()
paid_new = sum(v[0] for v in payments_after.values()) - sum(v[0] for v in payments_before.values())
if paid_new != len(created):
    abort('pagos nuevos %s distintos de los cargados %s' % (paid_new, len(created)))
cards_after = card_summary()
for state in set(cards_before) | set(cards_after) | {k[0] for k in card_delta}:
    b = cards_before.get(state, [0, 0.0])
    a = cards_after.get(state, [0, 0.0])
    if a[0] - b[0] != card_delta[(state, 'n')] or abs(a[1] - b[1] - card_delta[(state, 'amount')]) > 0.01:
        abort('el tablero no cambió solo por las cargas en %s: antes %s, después %s' % (state, b, a))
for partner in all_partners:
    change = partner._distri_account_balance() - balances_before[partner.id]
    if abs(change - expected_move.get(partner.id, 0.0)) > 0.01:
        abort('el saldo del cliente %s cambió %s y no %s' % (partner.ref, round(change, 2),
                                                              expected_move.get(partner.id, 0.0)))
    if abs(change) > 0.01 and partner._distri_account_balance() < -0.01:
        abort('el cliente %s quedó a favor' % partner.ref)

print('RESULT', json.dumps({
    **dict(stats), 'pagos_nuevos': len(created), 'importe': round(sum(created.mapped('amount')), 2),
    'clientes': len({p.partner_id.commercial_partner_id.id for p in created}),
    'pagos_antes': payments_before, 'pagos_despues': payments_after,
    'tablero_antes': cards_before, 'tablero_despues': cards_after,
}, ensure_ascii=False))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
