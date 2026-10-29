# Step 6 of the 24/10/2026 cross (odoo shell body, run by paso6-correcciones.sh).
#
# Corrections from the audit of the 24/10/2026 cross (auditoria/1_metodo.md).
# Every record touched gets a chatter note «Corrección por auditoría del
# 24/10/2026: <motivo>» (once: the note is the idempotency mark).
# 1. Lab payments already recorded in Online: the module's «Deshacer»
#    (action_undo: payment to draft, which unreconciles, then cancelled; card
#    back to draft), then the card closed as «Ya cargado en Online» with the
#    Online payment number, linked to the Online mirror when it is free.
#    Each Online line is checked first in online/creditos.json: same Online
#    customer, amount, date and move name.
# 2. Payments of the wrong customer, cancelled and registered again for the
#    right one with the same amount, date, journal and memo, oldest invoice
#    first: a Mercado Pago card through the module (action_undo, new customer,
#    action_confirm); a cash payment cancelled and created again like
#    cargar_efectivo_body.py does. The invoice the memo cites must belong to
#    the new customer in online/facturas.json.
# 3. Board cards the cross matched to an Online payment but the module left
#    pending or set aside (finding H3, auditoria/1_metodo_casos.csv): closed
#    as «Ya cargado en Online» with the cross's evidence (online_mp.csv, else
#    online_sin_mp_resuelto.csv). The «revisar» cards the module closed as
#    «Ya en Online» by amount only go back to pending, marked as not in Online
#    (online_unlinked: never linked by amount again); this runs first, so the
#    Online payments it frees can be linked to the card the cross names.
# 4. Lab copies of Online draft payments deleted from Online on 24/10
#    (online_id 6667..6806, the 140 paper sheets, cargas.jsonl): deleted only
#    if still draft and tied to no card.
# Guards (ABORT, nothing written): an Online line or invoice that does not
# match, a payment or card in an unexpected state, payments created != the
# ones recreated, card or payment totals that do not move by exactly the
# corrections, and any customer balance that does not move by exactly the
# corrected amounts (all customers are compared).
# Rolls back unless DO_COMMIT=1.
import csv
import json
import os
from collections import Counter
from datetime import date

from odoo.addons.distri_pedido.models.cobros import AMOUNT_TOLERANCE
from odoo.addons.distri_pedido.models.cuenta import clave_documento
from odoo.addons.distri_pedido.models.puesta_al_dia import ONLINE_POSTED
from odoo.addons.distri_pedido.models.reparto import _money

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IN = os.environ.get('IN_DIR', '/cruce')
MARK = 'Corrección por auditoría del 24/10/2026'
CROSS = 'Ya en Online según el cruce del 24/10'

# 1. lab payment id -> the Online payment it duplicates (move name, date)
ALREADY_IN_ONLINE = [
    {'payment': 1793, 'online': 'PCSH1/2026/01729', 'date': '2026-09-07'},
    {'payment': 1780, 'online': 'PCSH1/2026/01706', 'date': '2026-08-22'},
    {'payment': 1747, 'online': 'PBNK1/2026/01572', 'date': '2026-02-27',
     'created': '2026-05-28'},
]
# 2. payments registered for the wrong customer (lab refs ODOO-<Online id>)
WRONG_CUSTOMER = [
    {'payment': 1853, 'kind': 'mp', 'amount': 98100.0, 'from': 'ODOO-742', 'to': 'ODOO-740'},
    {'payment': 2358, 'kind': 'cash', 'amount': 132200.0, 'from': 'ODOO-145', 'to': 'ODOO-470',
     'invoice': '0001/2026/06619'},
]
# 4. Online draft payments deleted from Online on 24/10/2026
STALE_FROM, STALE_TO = 6667, 6806

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
    print('LOG', json.dumps(dict(paso=6, **data), ensure_ascii=False, default=str))


def skip(**data):
    print('SKIP', json.dumps(data, ensure_ascii=False, default=str))


def read_csv(name):
    with open(os.path.join(IN, name), encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def read_json(name):
    with open(os.path.join(IN, name), encoding='utf-8') as handle:
        return json.load(handle)


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


def online_id_of(partner):
    ref = partner.ref or ''
    return int(ref[5:]) if ref.startswith('ODOO-') and ref[5:].isdigit() else 0


def move_base(name):
    """«PBNK1/2026/00498 (0001/…)» -> «PBNK1/2026/00498»."""
    return (name or '').split(' ')[0]


def mirror_of(move_name):
    found = OnlinePayment.search(['|', ('name', '=', move_name), '|', ('move_name', '=', move_name),
                                  ('move_name', '=like', move_name + ' (%')])
    return found if len(found) == 1 else OnlinePayment.browse()


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


# ------------------------------------------------------------------ inputs
credits = {}
for line in read_json('creditos.json'):
    credits.setdefault(move_base(line['move_id'][1] if line.get('move_id') else ''), []).append(line)
invoices = {f['name']: f for f in read_json('facturas.json') if f.get('move_type') == 'out_invoice'}
cases = read_csv('1_metodo_casos.csv')
to_close = [c for c in cases if c['hallazgo'] == 'H3_borrador_lab_ya_en_online']
to_reopen = [c for c in cases if c['hallazgo'] == 'H3_revisar_cerrado_por_modulo']
if not to_close or not to_reopen:
    abort('1_metodo_casos.csv no tiene los casos del hallazgo H3')
online_mp = read_csv('online_mp.csv')
mp_by_op, mp_by_id = {}, {}
for row in online_mp:
    if row['mp_op']:
        mp_by_op.setdefault(row['mp_op'], []).append(row)
    mp_by_id[row['pago_id']] = row
resolved = {}
for row in read_csv('online_sin_mp_resuelto.csv'):
    if row['decision'] == 'encontrado':
        for op in row['mp_ops'].split('|'):
            resolved.setdefault(op, []).append(row)
sheet_ids = {json.loads(line)['online_id'] for line in open(os.path.join(IN, 'cargas.jsonl'),
                                                            encoding='utf-8') if line.strip()}

# ------------------------------------------------------------------ before
payments_before = payment_summary()
cards_before = card_summary()
all_partners = Partner.search([('ref', '=like', 'ODOO-%'), ('active', '=', True)]).filtered(
    lambda p: p == p.commercial_partner_id)
balances_before = {p.id: p._distri_account_balance() for p in all_partners}
expected_move = Counter()   # partner id -> expected balance change (+ = owes more)
payment_delta = Counter()   # (state, 'n'|'amount') -> expected change
card_delta = Counter()
created = Payment.browse()
stats = {k: Counter() for k in ('ya_en_online', 'otro_cliente', 'tarjetas_cruce',
                                'revisar_reabiertas', 'copias_online')}


def move_payment_state(payment, new_state):
    payment_delta[(payment.state, 'n')] -= 1
    payment_delta[(payment.state, 'amount')] -= payment.amount
    payment_delta[(new_state, 'n')] += 1
    payment_delta[(new_state, 'amount')] += payment.amount


def move_card_state(card, new_state):
    card_delta[(card.state, 'n')] -= 1
    card_delta[(card.state, 'amount')] -= card.amount
    card_delta[(new_state, 'n')] += 1
    card_delta[(new_state, 'amount')] += card.amount


def check_no_history_link(payment):
    if Mov.search_count([('lab_payment_id', '=', payment.id)]):
        abort('el pago %s tiene una fila de la historia de Online archivada a su nombre' % payment.id)


# ------------------------------------------------------------------ 1
for spec in ALREADY_IN_ONLINE:
    payment = Payment.browse(spec['payment']).exists()
    if not payment:
        abort('el pago %s no está en el lab' % spec['payment'])
    card = Collection.search([('payment_id', '=', payment.id)])
    if len(card) != 1:
        abort('el pago %s no tiene una única tarjeta del tablero' % payment.id)
    online_customer = online_id_of(payment.partner_id.commercial_partner_id)
    lines = [l for l in credits.get(spec['online'], [])
             if l['partner_id'] and l['partner_id'][0] == online_customer
             and abs(l['credit'] - payment.amount) <= 0.005 and l['date'] == spec['date']
             and (not spec.get('created') or l['create_date'][:10] == spec['created'])]
    if len(lines) != 1:
        abort('%s no está en creditos.json con el cliente %s de Online y %s' % (
            spec['online'], online_customer, _money(payment.amount)))
    mirror = mirror_of(spec['online'])
    reason = 'ya registrado en Online como %s' % spec['online']
    if payment.state == 'canceled':
        if card.state != 'ignored' or card.ignore_reason != 'online':
            abort('el pago %s está anulado pero su tarjeta %s no está cerrada' % (payment.id, card.id))
        stats['ya_en_online']['ya_corregido'] += 1
        continue
    if payment.state not in ('in_process', 'paid') or card.state != 'confirmed':
        abort('el pago %s (%s) o su tarjeta %s (%s) no está como se esperaba' % (
            payment.id, payment.state, card.id, card.state))
    check_no_history_link(payment)
    expected_move[payment.partner_id.commercial_partner_id.id] += payment.amount
    move_payment_state(payment, 'canceled')
    move_card_state(card, 'ignored')
    card.action_undo()
    if payment.state != 'canceled' or reconciled(payment) or card.state != 'draft':
        abort('el pago %s no quedó anulado y sin conciliar' % payment.id)
    vals = {'state': 'ignored', 'ignore_reason': 'online', 'ignore_note': reason[:1].upper() + reason[1:]}
    linked = bool(mirror and not mirror.collection_id and not card.online_payment_id
                  and abs(mirror.amount - card.amount) <= AMOUNT_TOLERANCE)
    if linked:
        vals['online_payment_id'] = mirror.id
        mirror.write({'collection_id': card.id})
    card.write(vals)
    text = '%s (%s del %s). Se anula el pago %s del lab para no contarlo dos veces.' % (
        reason, _money(payment.amount), day(date.fromisoformat(lines[0]['date'])), payment.name)
    note(card, text)
    note(payment, text)
    stats['ya_en_online']['anulado'] += 1
    log(accion='anulado_ya_en_online', pago=payment.id, nombre=payment.name, cobro=card.id,
        online=spec['online'], linea_online=lines[0]['id'], espejo=mirror.id if linked else None,
        importe=payment.amount)

# ------------------------------------------------------------------ 2
for spec in WRONG_CUSTOMER:
    old = Payment.browse(spec['payment']).exists()
    if not old or abs(old.amount - spec['amount']) > 0.005:
        abort('el pago %s no está en el lab o tiene otro importe' % spec['payment'])
    source, target = lab_partner(spec['from']), lab_partner(spec['to'])
    if spec.get('invoice'):
        invoice = invoices.get(spec['invoice'])
        if not invoice or invoice['partner_id'][0] != online_id_of(target):
            abort('la factura %s no es del cliente %s en facturas.json' % (spec['invoice'], spec['to']))
        if spec['invoice'] not in (old.memo or ''):
            abort('el memo del pago %s no cita la factura %s' % (old.id, spec['invoice']))
    reason_of = lambda new: 'el pago corresponde al cliente %s (Online %s), no al %s (Online %s)%s' % (
        target.name, online_id_of(target), source.name, online_id_of(source),
        '; la factura %s es de ese cliente' % spec['invoice'] if spec.get('invoice') else '') + (
        '. Se anula %s y se registra de nuevo como %s.' % (old.name, new.name) if new else '.')
    if spec['kind'] == 'mp':
        # the memo of a Mercado Pago payment starts with «Mercado Pago <operation>»
        words = (old.memo or '').split()
        operation = words[2] if words[:2] == ['Mercado', 'Pago'] and len(words) > 2 else '-'
        card = Collection.search([('kind', '=', 'income'), ('source_id', '=', operation)])
        if len(card) != 1:
            abort('el pago %s no tiene una única tarjeta del tablero' % old.id)
        if old.state == 'canceled':
            if card.state == 'confirmed' and card.partner_id.commercial_partner_id == target \
                    and card.payment_id != old:
                stats['otro_cliente']['ya_corregido'] += 1
                continue
            abort('el pago %s está anulado y su tarjeta %s no quedó para %s' % (old.id, card.id, spec['to']))
        if card.payment_id != old or card.state != 'confirmed' or \
                old.partner_id.commercial_partner_id != source:
            abort('el pago %s no está confirmado en su tarjeta para %s' % (old.id, spec['from']))
        check_no_history_link(old)
        move_payment_state(old, 'canceled')
        card.action_undo()
        if old.state != 'canceled' or reconciled(old) or card.state != 'draft':
            abort('el pago %s no quedó anulado y sin conciliar' % old.id)
        card.write({'partner_id': target.id, 'proposal': 'manual',
                    'proposal_note': ('%s: cliente corregido' % MARK)[:250]})
        card.action_confirm()
        new = card.payment_id
        if card.state != 'confirmed' or not new or new == old:
            abort('la tarjeta %s no quedó confirmada de nuevo' % card.id)
        note(card, reason_of(new))
    else:
        key = 'distri_corr_20261024_pago_%s' % old.id
        done = Payment.search([('payment_reference', '=', key)])
        if old.state == 'canceled':
            if len(done) == 1 and done.partner_id.commercial_partner_id == target:
                stats['otro_cliente']['ya_corregido'] += 1
                continue
            abort('el pago %s está anulado y no hay uno nuevo para %s' % (old.id, spec['to']))
        if done or old.state not in ('in_process', 'paid') or \
                old.partner_id.commercial_partner_id != source:
            abort('el pago %s no está como se esperaba' % old.id)
        check_no_history_link(old)
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
    if (new.amount, new.date, new.journal_id, new.memo, new.payment_type) != \
            (old.amount, old.date, old.journal_id, old.memo, old.payment_type) or \
            new.move_id.state != 'posted' or new.partner_id.commercial_partner_id != target:
        abort('el pago nuevo %s no repite importe, fecha, diario y memo de %s' % (new.id, old.id))
    receivable = new.move_id.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
    twin = clave_documento(new.move_id.name, sum(receivable.mapped('debit')),
                           sum(receivable.mapped('credit')))
    if Mov.search([('partner_id', '=', target.id)]).filtered(
            lambda m: clave_documento(m.comprobante, m.debe, m.haber) == twin):
        abort('el asiento %s coincide con un movimiento de la historia de Online' % new.move_id.name)
    note(old, reason_of(new))
    note(new, reason_of(new))
    created |= new
    payment_delta[(new.state, 'n')] += 1
    payment_delta[(new.state, 'amount')] += new.amount
    expected_move[source.id] += old.amount
    expected_move[target.id] -= new.amount
    stats['otro_cliente']['movido'] += 1
    log(accion='otro_cliente', pago_anulado=old.id, pago_nuevo=new.id, nombre=new.name,
        asiento=new.move_id.id, de=spec['from'], a=spec['to'], importe=new.amount)

# ------------------------------------------------------------------ 3b (first: frees Online payments)
for row in to_reopen:
    card = Collection.search([('kind', '=', 'income'), ('source_id', '=', row['mp_op'])])
    if len(card) != 1 or abs(card.amount - float(row['importe_mp'])) > 0.005:
        abort('la operación %s no está en el tablero con ese importe' % row['mp_op'])
    if card.state == 'draft' and card.online_unlinked and noted(card):
        stats['revisar_reabiertas']['ya_corregido'] += 1
        continue
    pay = card.online_payment_id
    if card.state != 'ignored' or card.ignore_reason != 'online' or not pay or card.export_batch_id \
            or card.payment_id:
        abort('la operación %s («revisar») no está cerrada como Ya en Online' % row['mp_op'])
    label = pay.name or move_base(pay.move_name) or 'pago %s de Online' % pay.online_id
    move_card_state(card, 'draft')
    pay.write({'collection_id': False, 'link_kind': False})
    card.write({'online_payment_id': False, 'online_unlinked': True, 'online_conflict': False,
                'state': 'draft', 'ignore_reason': False, 'ignore_note': False})
    note(card, 'vuelve a revisión: el sistema lo unió solo por importe a %s del %s (%s) y el cruce '
               'lo deja para revisar. Si el cliente propuesto salió de ese pago, confirmarlo antes '
               'de cargar.' % (label, day(pay.date), _money(pay.amount)))
    stats['revisar_reabiertas']['reabierta'] += 1
    log(accion='revisar_reabierta', op=row['mp_op'], cobro=card.id, pago_online=pay.id)

# ------------------------------------------------------------------ 3a
for row in to_close:
    card = Collection.search([('kind', '=', 'income'), ('source_id', '=', row['mp_op'])])
    if len(card) != 1 or abs(card.amount - float(row['importe_mp'])) > 0.005:
        abort('la operación %s no está en el tablero con ese importe' % row['mp_op'])
    evidence = [r for r in mp_by_op.get(row['mp_op'], []) if move_base(r['pago']) == row['referencia']]
    how = ''
    if len(evidence) == 1:
        how = 'vínculo %s, %s candidato(s)' % (evidence[0]['como'], evidence[0]['candidatos'])
    else:
        found = [mp_by_id.get(r['pago_id']) for r in resolved.get(row['mp_op'], [])]
        evidence = [r for r in found if r and move_base(r['pago']) == row['referencia']]
        if len(evidence) == 1:
            how = 'resuelto: %s' % [r for r in resolved[row['mp_op']]
                                    if r['pago_id'] == evidence[0]['pago_id']][0]['motivo']
    if len(evidence) != 1:
        abort('la operación %s no tiene en el cruce el pago %s de Online' % (row['mp_op'], row['referencia']))
    ev = evidence[0]
    if abs(float(ev['importe']) - card.amount) > AMOUNT_TOLERANCE:
        abort('el pago %s de Online tiene otro importe que la operación %s' % (ev['pago'], row['mp_op']))
    if card.state == 'ignored' and card.ignore_reason == 'online' and \
            (card.ignore_note or '').startswith(CROSS):
        stats['tarjetas_cruce']['ya_corregido'] += 1
        continue
    if card.state not in ('draft', 'apartado') or card.payment_id or card.online_payment_id:
        if card.state == 'confirmed':
            abort('la operación %s está confirmada y el cruce la da por cargada en Online' % row['mp_op'])
        stats['tarjetas_cruce']['otro_estado:%s' % card.state] += 1
        skip(op=row['mp_op'], cobro=card.id, motivo='estado %s' % card.state)
        continue
    online_day = ev['fecha'][8:10] + '/' + ev['fecha'][5:7] + '/' + ev['fecha'][:4]
    detail = '%s del %s (%s; %s)' % (row['referencia'], online_day, _money(float(ev['importe'])), how)
    mirror = mirror_of(row['referencia'])
    linked = bool(mirror and not mirror.collection_id
                  and abs(mirror.amount - card.amount) <= AMOUNT_TOLERANCE)
    was = card.state
    move_card_state(card, 'ignored')
    vals = {'state': 'ignored', 'ignore_reason': 'online',
            'ignore_note': ('%s: %s' % (CROSS, detail))[:250]}
    if linked:
        vals['online_payment_id'] = mirror.id
        mirror.write({'collection_id': card.id})
    card._close_alerts('%s: ya en Online' % MARK)
    card.write(vals)
    note(card, 'ya en Online según el cruce del 24/10: %s. Estaba %s en el lab sin nota; se '
               'descarta para no cobrarlo dos veces.' % (
                   detail, 'pendiente' if was == 'draft' else 'apartado como mayorista'))
    stats['tarjetas_cruce']['%s:%s' % (was, 'vinculada' if linked else 'sin_vinculo')] += 1
    log(accion='ya_en_online_cruce', op=row['mp_op'], cobro=card.id, estado_anterior=was,
        online=row['referencia'], espejo=mirror.id if linked else None, importe=card.amount)

# ------------------------------------------------------------------ 4
stale = OnlinePayment.search([('online_id', '>=', STALE_FROM), ('online_id', '<=', STALE_TO)])
for pay in stale:
    if pay.state != 'draft' or pay.collection_id or pay.online_id not in sheet_ids or \
            Collection.search_count([('online_payment_id', '=', pay.id)]):
        abort('la copia del pago %s de Online no está en borrador, libre y en las planillas' % pay.online_id)
for pay in stale:
    log(accion='copia_online_borrada', espejo=pay.id, online_id=pay.online_id, nombre=pay.name,
        fecha=pay.date, importe=pay.amount, cliente_online=pay.online_partner_id)
stats['copias_online']['borradas'] = len(stale)
stats['copias_online']['importe'] = round(sum(stale.mapped('amount')), 2)
if not stale:
    stats['copias_online']['ya_corregido'] = 1
stale.unlink()

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
    'ya_en_online': dict(stats['ya_en_online']), 'otro_cliente': dict(stats['otro_cliente']),
    'tarjetas_cruce': dict(stats['tarjetas_cruce']),
    'revisar_reabiertas': dict(stats['revisar_reabiertas']),
    'copias_online': dict(stats['copias_online']),
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
