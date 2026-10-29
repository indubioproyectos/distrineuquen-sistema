# Step 8 of the 24/10/2026 cross (odoo shell body, run by paso8-auditoria-general.sh).
#
# Corrections from the general audit of 25/10/2026 (auditoria/general/hallazgos.md),
# read from paso8_casos.csv. Every record touched gets a chatter note
# «Corrección por auditoría del 25/10/2026: <motivo>» (once: the note is the
# idempotency mark).
# 1. Cash payment 2528 back to ODOO-145: step 6 moved it to ODOO-470, which in
#    Online is a delivery contact of 145 (the lab has it as a separate
#    customer with no history). The invoice its memo cites is in the lab
#    history of 145. Cancelled and created again like step 6 does.
# 2. Board cards the module closed as «Ya en Online» by amount only, against
#    an Online payment the cross assigns with evidence to another operation:
#    reopened as not in Online (online_unlinked: never linked by amount
#    again); «reabrir_revisar» back to the board, «reabrir_apartar» set aside
#    as wholesale. The freed Online payment is linked to the card of the
#    operation the cross names when that card is closed as «Ya en Online»
#    without a link (only when the cross names one operation; with two, the
#    free card can be the wrong one), and the Online payment step 7 frees to
#    its operation.
# 3. Pending cards of transfers made by employees: closed as «No es el pago
#    de un cliente» (cash hand-in); the two from the manager's own account as
#    «Movimiento propio». The other employees' transfers stay pending with a
#    note: a cash hand-in, goods taken without an invoice, or a customer's
#    payment (Online often credits Italo's to the customer).
# 4. Lab payments that may be a hand-in or a double with cash: a review note
#    only, nothing changes (decision for management).
# Guards (ABORT, nothing written): step 7 not committed, a card or payment in
# an unexpected state, card or payment totals that do not move by exactly the
# corrections, and any customer balance that does not move by exactly the
# corrected amounts (all customers are compared).
# Rolls back unless DO_COMMIT=1.
import csv
import json
import os
from collections import Counter

from odoo.addons.distri_pedido.models.cobros import AMOUNT_TOLERANCE
from odoo.addons.distri_pedido.models.reparto import _money

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IN = os.environ.get('IN_DIR', '/cruce')
MARK = 'Corrección por auditoría del 25/10/2026'
STEP7_OP = '170642515062'
# 1.
MOVE_BACK = {'payment': 2528, 'amount': 132200.0, 'from': 'ODOO-470', 'to': 'ODOO-145',
             'invoice': '0001/2026/06619'}
# 3. who made the transfer, as the note names them
EMPLOYEE = {'italo': 'Italo', 'simon': 'Simón', 'jorge': 'Jorge', 'admin': 'el administrador',
            'gerente': 'el gerente'}
# 4. lab payment -> review note
REVIEW = {
    1765: 'transferencia desde el CUIT del administrador a su propia cuenta de cliente',
    1790: 'transferencia desde el CUIT del administrador a su propia cuenta de cliente',
    1808: 'transferencia desde el CUIT del administrador a su propia cuenta de cliente',
    1775: 'transferencia de Italo acreditada al cliente que nombra su comprobante',
    1834: 'transferencia de Italo acreditada al cliente que nombra su comprobante',
    1954: 'transferencia de Italo acreditada al cliente que nombra su comprobante',
    2100: 'posible doble con el efectivo 2478 (mismo cliente e importe, 20/10 por MP y 21/10 en planilla)',
}
# 3b. Italo's pending transfers: a note only (Online often credits them to the
# customer); two have an Online credit of the same day and amount
ITALO_ONLINE = {'154115721217': 'PBNK1/2026/00839', '152624392123': 'PBNK1/2026/00776'}
# the Online payment step 7 frees, and the operation it belongs to
EXTRA_LINKS = [('170974847823', 'PBNK1/2026/02717')]
REVIEW_TAIL = ('. Puede ser una rendición de efectivo y no un pago del cliente: queda para decidir '
               'con gerencia y administración; no se modifica.')

Partner = env['res.partner'].sudo().with_context(active_test=False)
Payment = env['account.payment'].sudo()
Collection = env['distri.mp.collection'].sudo()
OnlinePayment = env['distri.online.payment'].sudo()
Mov = env['distri.cuenta.movimiento'].sudo().with_context(active_test=False)
Message = env['mail.message'].sudo()


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


def log(**data):
    print('LOG', json.dumps(dict(paso=8, **data), ensure_ascii=False, default=str))


def skip(**data):
    print('SKIP', json.dumps(data, ensure_ascii=False, default=str))


def noted(record):
    return bool(Message.search_count([('model', '=', record._name), ('res_id', '=', record.id),
                                      ('body', 'ilike', MARK)]))


def note(record, reason):
    if not noted(record):
        record.message_post(body='%s: %s' % (MARK, reason))


def lab_partner(ref):
    found = Partner.search([('ref', '=', ref)])
    commercial = found.commercial_partner_id
    if len(commercial) != 1 or not commercial.active or commercial.distri_generic_partner:
        abort('cliente %s: %s en el lab' % (
            ref, 'no está' if not found else 'no es único, está archivado o es genérico'))
    return commercial


def move_base(name):
    return (name or '').split(' ')[0]


def day(value):
    return value.strftime('%d/%m/%Y') if value else 's/f'


def reconciled(payment):
    lines = payment.move_id.line_ids
    return bool(lines.matched_debit_ids or lines.matched_credit_ids)


def summary_sql(query):
    env.flush_all()
    env.cr.execute(query)
    return {row[0]: [row[1], round(float(row[2] or 0), 2)] for row in env.cr.fetchall()}


def payment_summary():
    return summary_sql('select state, count(*), sum(amount) from account_payment group by 1')


def card_summary():
    return summary_sql("select state, count(*), sum(amount) from distri_mp_collection "
                       "where kind = 'income' group by 1")


def card_of(row):
    card = Collection.browse(int(row['cobro'])).exists() if row['cobro'] else Collection.browse()
    if not card or card.kind != 'income' or card.source_id != row['op'] or \
            abs(card.amount - float(row['importe'])) > 0.005:
        abort('la tarjeta %s no es la operación %s por %s' % (row['cobro'], row['op'], row['importe']))
    return card


with open(os.path.join(IN, 'paso8_casos.csv'), encoding='utf-8') as handle:
    rows = list(csv.DictReader(handle))
if Counter(r['accion'] for r in rows) != Counter(
        {'nota_tarjeta': 59, 'reabrir_apartar': 33, 'reabrir_revisar': 39,
         'nota_revision': 7}):
    abort('paso8_casos.csv no tiene los casos de la auditoría general')

# ------------------------------------------------------------------ before
step7 = Collection.search([('kind', '=', 'income'), ('source_id', '=', STEP7_OP)])
if os.environ.get('PROBAR_SIN_PASO7') == '1' and not DO_COMMIT:
    print('WARN prueba sin el paso 7 guardado')
elif len(step7) != 1 or step7.state != 'confirmed' or not step7.payment_id:
    abort('el paso 7 no está guardado: la operación %s no está confirmada' % STEP7_OP)
payments_before = payment_summary()
cards_before = card_summary()
all_partners = Partner.search([('ref', '=like', 'ODOO-%'), ('active', '=', True)]).filtered(
    lambda p: p == p.commercial_partner_id)
balances_before = {p.id: p._distri_account_balance() for p in all_partners}
expected_move = Counter()
payment_delta = Counter()
card_delta = Counter()
created = Payment.browse()
stats = {k: Counter() for k in ('pago_devuelto', 'reabiertas', 'online_revinculado', 'rendicion',
                                'propia', 'nota_tarjeta', 'nota_revision')}


def move_payment_state(payment, new_state):
    payment_delta[(payment.state, 'n')] -= 1
    payment_delta[(payment.state, 'amount')] -= payment.amount
    payment_delta[(new_state, 'n')] += 1
    payment_delta[(new_state, 'amount')] += payment.amount


def move_card_state(card, old_state):
    card_delta[(old_state, 'n')] -= 1
    card_delta[(old_state, 'amount')] -= card.amount
    card_delta[(card.state, 'n')] += 1
    card_delta[(card.state, 'amount')] += card.amount


# ------------------------------------------------------------------ 1
spec = MOVE_BACK
old = Payment.browse(spec['payment']).exists()
if not old or abs(old.amount - spec['amount']) > 0.005 or old.journal_id.code != 'CSH1':
    abort('el pago %s no está en el lab o tiene otro importe' % spec['payment'])
source, target = lab_partner(spec['from']), lab_partner(spec['to'])
key = 'distri_corr_20261025_pago_%s' % old.id
done = Payment.search([('payment_reference', '=', key)])
if old.state == 'canceled':
    if len(done) != 1 or done.partner_id.commercial_partner_id != target:
        abort('el pago %s está anulado y no hay uno nuevo para %s' % (old.id, spec['to']))
    stats['pago_devuelto']['ya_corregido'] += 1
else:
    if done or old.state not in ('in_process', 'paid') or \
            old.partner_id.commercial_partner_id != source:
        abort('el pago %s no está como se esperaba' % old.id)
    if spec['invoice'] not in (old.memo or ''):
        abort('el memo del pago %s no cita la factura %s' % (old.id, spec['invoice']))
    if not Mov.search_count([('partner_id', '=', target.id), ('comprobante', '=', spec['invoice'])]):
        abort('la factura %s no está en la historia del cliente %s' % (spec['invoice'], spec['to']))
    if Mov.search_count([('partner_id', '=', source.id)]):
        abort('el cliente %s tiene historia propia: revisar antes de mover' % spec['from'])
    if Mov.search_count([('lab_payment_id', '=', old.id)]):
        abort('el pago %s tiene una fila de la historia de Online archivada a su nombre' % old.id)
    move_payment_state(old, 'canceled')
    old.action_draft()
    old.action_cancel()
    if old.state != 'canceled' or reconciled(old):
        abort('el pago %s no quedó anulado y sin conciliar' % old.id)
    new = Payment.create({
        'payment_type': 'inbound', 'partner_type': 'customer', 'partner_id': target.id,
        'amount': old.amount, 'journal_id': old.journal_id.id, 'date': old.date,
        'memo': old.memo, 'payment_reference': key, 'company_id': old.company_id.id,
    })
    new.action_post()
    env['distri.route'].sudo()._apply_oldest_first(new, target)
    if (new.amount, new.date, new.journal_id, new.memo) != (old.amount, old.date, old.journal_id, old.memo) \
            or new.move_id.state != 'posted' or new.partner_id.commercial_partner_id != target:
        abort('el pago nuevo %s no repite importe, fecha, diario y memo de %s' % (new.id, old.id))
    reason = ('el pago corresponde al cliente %s (Online 145): el %s (Online 470) es un punto de '
              'entrega de ese cliente en Online y la factura %s está en su historia. El paso 6 lo '
              'había movido por error. Se anula %s y se registra de nuevo como %s.' % (
                  target.name, source.name, spec['invoice'], old.name, new.name))
    note(old, reason)
    note(new, reason)
    created |= new
    payment_delta[(new.state, 'n')] += 1
    payment_delta[(new.state, 'amount')] += new.amount
    expected_move[source.id] += old.amount
    expected_move[target.id] -= new.amount
    stats['pago_devuelto']['movido'] += 1
    log(accion='pago_devuelto', pago_anulado=old.id, pago_nuevo=new.id, nombre=new.name,
        de=spec['from'], a=spec['to'], importe=new.amount)

# ------------------------------------------------------------------ 2
freed = []
for row in [r for r in rows if r['accion'] in ('reabrir_revisar', 'reabrir_apartar')]:
    card = card_of(row)
    if card.online_unlinked and noted(card) and card.state in ('draft', 'apartado'):
        stats['reabiertas']['ya_corregido'] += 1
        continue
    pay = card.online_payment_id
    if card.state != 'ignored' or card.ignore_reason != 'online' or not pay or card.payment_id \
            or card.export_batch_id:
        abort('la operación %s no está cerrada como Ya en Online' % row['op'])
    label = pay.name or move_base(pay.move_name)
    if row['pago_online'] not in (pay.name, move_base(pay.move_name)):
        abort('la operación %s está unida a %s y no a %s' % (row['op'], label, row['pago_online']))
    was = card.state
    pay.write({'collection_id': False, 'link_kind': False})
    card.write({'online_payment_id': False, 'online_unlinked': True, 'online_conflict': False,
                'state': 'draft', 'ignore_reason': False, 'ignore_note': False})
    others = [o for o in row['op_correcta'].split(';') if o]
    why = ('no está en Online. El sistema lo había unido solo por importe a %s del %s (%s), que el '
           'cruce asigna con evidencia a %s %s.' % (
               label, day(pay.date), _money(pay.amount),
               'la operación' if len(others) == 1 else 'una de las operaciones', ' y '.join(others)))
    if row['accion'] == 'reabrir_apartar':
        note(card, why + ' El pagador es mayorista: se aparta.')
        card._distri_set_aside(label='Cruce 25/10: pagador mayorista')
        if card.state != 'apartado':
            abort('la operación %s no quedó apartada' % row['op'])
    else:
        note(card, why + ' Vuelve al tablero para identificar al cliente.')
        card._propose()
        if card.state not in ('draft', 'apartado') or card.payment_id:
            abort('la operación %s no quedó pendiente' % row['op'])
    move_card_state(card, was)
    stats['reabiertas']['%s:%s' % (row['accion'], card.state)] += 1
    freed.append((pay, row))
    log(accion=row['accion'], op=row['op'], cobro=card.id, estado=card.state, pago_online=pay.id)

for pay, row in freed:
    if pay.collection_id:
        continue
    correct = [o for o in row['op_correcta'].split(';') if o]
    if len(correct) != 1:
        # two operations share the credit: the free card of equal amount can be
        # the other one, whose payer pays another customer (blind check 25/10)
        stats['online_revinculado']['dos_operaciones'] += 1
        continue
    targets = Collection.search([('kind', '=', 'income'),
                                 ('source_id', 'in', correct),
                                 ('state', '=', 'ignored'), ('ignore_reason', '=', 'online'),
                                 ('online_payment_id', '=', False)])
    targets = targets.filtered(lambda c: abs(c.amount - pay.amount) <= AMOUNT_TOLERANCE)
    if len(targets) != 1:
        stats['online_revinculado']['sin_tarjeta'] += 1
        continue
    targets.online_payment_id = pay
    pay.collection_id = targets
    note(targets, 'se une al pago %s de Online, que estaba unido por importe a la operación %s.' % (
        pay.name or move_base(pay.move_name), row['op']))
    stats['online_revinculado']['vinculado'] += 1
    log(accion='online_revinculado', pago_online=pay.id, cobro=targets.id, op=targets.source_id)

# ------------------------------------------------------------------ 3
for row in [r for r in rows if r['accion'] in ('rendicion', 'propia')]:
    card = card_of(row)
    if card.state == 'ignored' and noted(card):
        stats[row['accion']]['ya_corregido'] += 1
        continue
    if card.receipt_id:
        abort('la operación %s tiene comprobante: no se cierra como rendición' % row['op'])
    if card.state != 'draft' or card.payment_id:
        stats[row['accion']]['otro_estado:%s' % card.state] += 1
        skip(op=row['op'], cobro=card.id, motivo='estado %s' % card.state)
        continue
    who = EMPLOYEE.get(row['detalle'])
    if not who:
        abort('la operación %s no dice qué empleado transfirió' % row['op'])
    if row['accion'] == 'rendicion':
        reason, text = 'not_customer', 'Rendición de efectivo de %s por transferencia' % who
        body = ('transferencia de %s: es una rendición de efectivo y no el pago de un cliente. Los '
                'cobros en efectivo que cubre se cargan por la planilla del cobrador.' % who)
    else:
        reason, text = 'own', 'Transferencia desde la cuenta de %s' % who
        body = 'transferencia desde la cuenta de %s: movimiento propio, no es el pago de un cliente.' % who
    was = card.state
    card._close_alerts('%s: %s' % (MARK, text))
    card.write({'state': 'ignored', 'ignore_reason': reason, 'ignore_note': ('%s (auditoría 25/10)' % text)[:250]})
    note(card, body)
    move_card_state(card, was)
    stats[row['accion']][row['detalle']] += 1
    log(accion=row['accion'], op=row['op'], cobro=card.id, empleado=row['detalle'], importe=card.amount)

# ------------------------------------------------------------------ 3b
for row in [r for r in rows if r['accion'] == 'nota_tarjeta']:
    card = card_of(row)
    if card.state != 'draft' or card.payment_id:
        abort('la operación %s del empleado no está pendiente' % row['op'])
    who = EMPLOYEE.get(row['detalle'])
    if not who:
        abort('la operación %s no dice qué empleado transfirió' % row['op'])
    extra = ITALO_ONLINE.get(row['op'])
    note(card, 'transferencia de %s: puede ser una rendición de efectivo, el pago de mercadería que '
               'se llevó sin factura o el pago de un cliente que transfirió por él%s%s. Queda '
               'pendiente para revisar; no se modifica.' % (
                   who, ' (Online suele acreditar estas transferencias al cliente)'
                   if row['detalle'] == 'italo' else '',
                   '; hay un cobro de Online del mismo día e importe a nombre de Italo, %s' % extra
                   if extra else ''))
    stats['nota_tarjeta']['nota'] += 1

for op, move_name in EXTRA_LINKS:
    card = Collection.search([('kind', '=', 'income'), ('source_id', '=', op)])
    pay = OnlinePayment.search(['|', ('name', '=', move_name), ('move_name', '=like', move_name + '%')])
    if len(card) != 1 or len(pay) != 1:
        abort('no se encuentra la operación %s o el pago %s de Online' % (op, move_name))
    if card.online_payment_id == pay:
        stats['online_revinculado']['ya_corregido'] += 1
        continue
    if pay.collection_id and os.environ.get('PROBAR_SIN_PASO7') == '1' and not DO_COMMIT:
        print('WARN prueba sin el paso 7: %s sigue unido a otra operación' % move_name)
        continue
    if card.state != 'ignored' or card.ignore_reason != 'online' or card.online_payment_id or \
            pay.collection_id or abs(card.amount - pay.amount) > AMOUNT_TOLERANCE:
        abort('la operación %s o el pago %s no están libres para unirse' % (op, move_name))
    card.online_payment_id = pay
    pay.collection_id = card
    note(card, 'se une al pago %s de Online, que el paso 7 liberó de la operación %s.' % (
        move_name, STEP7_OP))
    stats['online_revinculado']['paso7'] += 1
    log(accion='online_revinculado', pago_online=pay.id, cobro=card.id, op=op)

# ------------------------------------------------------------------ 4
for row in [r for r in rows if r['accion'] == 'nota_revision']:
    payment = Payment.browse(int(row['pago_lab'])).exists()
    if not payment or payment.id not in REVIEW or abs(payment.amount - float(row['importe'])) > 0.005 \
            or payment.state not in ('in_process', 'paid'):
        abort('el pago %s no está como se esperaba' % row['pago_lab'])
    text = 'en revisión: %s%s' % (REVIEW[payment.id], REVIEW_TAIL)
    note(payment, text)
    for card in Collection.search([('payment_id', '=', payment.id)]):
        note(card, text)
    if payment.id == 2100:
        twin = Payment.browse(2478).exists()
        if not twin or twin.journal_id.code != 'CSH1' or abs(twin.amount - payment.amount) > 0.005 or \
                twin.partner_id.commercial_partner_id != payment.partner_id.commercial_partner_id:
            abort('el efectivo 2478 no es del mismo cliente e importe que el pago 2100')
        note(twin, 'en revisión: posible doble con el pago 2100 de Mercado Pago (mismo cliente e '
                   'importe, 20/10 por MP y 21/10 en planilla)%s' % REVIEW_TAIL)
    stats['nota_revision']['nota'] += 1

# ------------------------------------------------------------------ guards
env.flush_all()


def apply_delta(before, delta):
    result = {k: list(v) for k, v in before.items()}
    for (state, what), value in delta.items():
        entry = result.setdefault(state, [0, 0.0])
        entry[0 if what == 'n' else 1] += value
    return {k: [v[0], round(v[1], 2)] for k, v in result.items() if v[0] or abs(v[1]) > 0.005}


def same(a, b):
    a = {k: v for k, v in a.items() if v[0]}
    b = {k: v for k, v in b.items() if v[0]}
    return a.keys() == b.keys() and all(a[k][0] == b[k][0] and abs(a[k][1] - b[k][1]) <= 0.01 for k in a)


payments_after = payment_summary()
if not same(payments_after, apply_delta(payments_before, payment_delta)):
    abort('los pagos no cambiaron solo por las correcciones: antes %s, después %s' % (
        payments_before, payments_after))
if sum(v[0] for v in payments_after.values()) - sum(v[0] for v in payments_before.values()) != len(created):
    abort('pagos nuevos distintos de los registrados de nuevo')
cards_after = card_summary()
if not same(cards_after, apply_delta(cards_before, card_delta)):
    abort('el tablero no cambió solo por las correcciones: antes %s, después %s' % (
        cards_before, cards_after))
moved = 0
for partner in all_partners:
    change = partner._distri_account_balance() - balances_before[partner.id]
    if abs(change - expected_move.get(partner.id, 0.0)) > 0.01:
        abort('el saldo del cliente %s cambió %s y no %s' % (
            partner.id, round(change, 2), expected_move.get(partner.id, 0.0)))
    moved += abs(change) > 0.01
if any(abs(v) > 0.01 for p, v in expected_move.items() if p not in balances_before):
    abort('un cliente corregido no está entre los clientes de Online')

print('RESULT', json.dumps({
    **{k: dict(v) for k, v in stats.items()},
    'pagos_antes': payments_before, 'pagos_despues': payments_after,
    'tablero_antes': cards_before, 'tablero_despues': cards_after,
    'saldos_movidos': moved, 'saldos_esperados': {str(k): round(v, 2) for k, v in expected_move.items() if v},
}, ensure_ascii=False))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
