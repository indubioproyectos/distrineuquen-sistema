# Step 4 of the 24/10/2026 cross (odoo shell body, run by paso4-cargar-efectivo.sh).
#
# Cash payments into the lab, as the route settlement registers the street
# cash (reparto.py `_register_stop_cash`): one inbound customer payment,
# posted, applied oldest invoice first (`distri.route._apply_oldest_first`).
# Journal: «Efectivo» (CSH1), the route's default journal for the cash the
# drivers and collectors bring (`distri.route._default_journal`); never
# «Caja depósito» (CDEP), which holds only the cash taken at the depot and is
# closed per user in «Cierre de caja depósito».
#   - the 140 payments of the paper sheets (cargas.jsonl; the memo with the
#     collector and the sheet day from memos.jsonl), removed from Online on
#     24/10/2026: the read of that day no longer has any of them;
#   - the cash reported in the WhatsApp group (efectivo_chat.csv): only rows
#     with en_online = «no», an amount, a customer and a date from 18/03/2026;
#     low-confidence rows are skipped until confirmed.
# Memo: who collected it and the source («planilla dd/mm» or «aviso WhatsApp
# dd/mm hh:mm»). Payment reference: a key of the load (distri_cobro_ef_… of
# the sheet loader, distri_cobro_efwa_… for the chat); a payment with that key
# is never created again (idempotent).
# Not loaded, listed (SKIP): a sheet payment whose Online id is still posted
# in the mirror (it already counts through the Online history); a payment
# with a posted Online payment of the same customer and amount from 3 days
# before to 10 days after (possible double); a chat notice with a sheet
# payment of the same customer and amount in that window (the same cash).
# Guards (ABORT, nothing written): a customer not found, a payment without
# its journal entry, payments created != rows loaded, totals or customer
# balances that do not move by exactly the loaded amounts.
# Rolls back unless DO_COMMIT=1.
import csv
import json
import os
import re
from datetime import date, datetime, timedelta

from odoo.addons.distri_pedido.models.cobros import AMOUNT_TOLERANCE
from odoo.addons.distri_pedido.models.cuenta import clave_documento
from odoo.addons.distri_pedido.models.puesta_al_dia import ONLINE_POSTED

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IN = os.environ.get('IN_DIR', '/cruce')
CHAT_SINCE = '2026-03-18'
BEFORE, AFTER = timedelta(days=3), timedelta(days=10)
SHEET_MEMO_RE = re.compile(r'^(?P<doc>.+?) · (?P<who>.+) (?P<day>\d{2}/\d{2})$')
CASH_CODE = 'CSH1'

Partner = env['res.partner'].sudo().with_context(active_test=False)
Payment = env['account.payment'].sudo()
OnlinePayment = env['distri.online.payment'].sudo()
Mov = env['distri.cuenta.movimiento'].sudo()


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


def log(**data):
    print('LOG', json.dumps(dict(paso=4, **data), ensure_ascii=False, default=str))


def skip(**data):
    print('SKIP', json.dumps(data, ensure_ascii=False, default=str))


def jsonl(name):
    with open(os.path.join(IN, name), encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


journal = env['account.journal'].sudo().search([('code', '=', CASH_CODE), ('type', '=', 'cash'),
                                                ('company_id', '=', env.company.id)])
if len(journal) != 1:
    abort('no hay un único diario de efectivo %s' % CASH_CODE)

partners = {}


def lab_partner(online_id):
    if online_id not in partners:
        found = Partner.search([('ref', '=', 'ODOO-%s' % online_id)])
        commercial = found.commercial_partner_id
        if len(commercial) != 1 or not commercial.active or commercial.distri_generic_partner:
            abort('cliente de Online %s: %s en el lab' % (
                online_id, 'no está' if not found else 'no es único, está archivado o es genérico'))
        partners[online_id] = commercial
    return partners[online_id]


# ------------------------------------------------------------------ items
memos = {m['clave']: m['memo_nuevo'] for m in jsonl('memos.jsonl')}
items = []
for row in jsonl('cargas.jsonl'):
    text = memos.get(row['clave']) or row['memo']
    found = SHEET_MEMO_RE.match(text)
    if not found:
        abort('memo de planilla sin repartidor y día: %s' % row['clave'])
    doc = found['doc'] if found['doc'].lower() != 'sin factura' else 'sin factura'
    items.append({
        'source': 'planilla', 'key': row['clave'], 'online_id': row['online_id'],
        'online_partner': int(row['partner_id']), 'amount': float(row['importe']),
        'date': date.fromisoformat(row['fecha']),
        'memo': 'Efectivo · cobró %s · planilla %s · %s' % (found['who'], found['day'], doc),
    })
with open(os.path.join(IN, 'efectivo_chat.csv'), encoding='utf-8') as handle:
    chat = [r for r in csv.DictReader(handle) if r['en_online'] == 'no' and r['importe']
            and r['cliente_id'] and r['fecha'] >= CHAT_SINCE]
for row in chat:
    when = datetime.strptime(row['fecha'], '%Y-%m-%d %H:%M')
    amount = float(row['importe'])
    if row['confianza'] == 'baja':
        # left out until the manager and the administrator confirm them (ale, 24/10/2026);
        # the note may name the customer: never printed
        print('SKIP', json.dumps({'chat': row['fecha'], 'cliente_online': row['cliente_id'],
                                  'motivo': 'confianza baja: confirmar con gerente y administrador'},
                                 ensure_ascii=False))
        continue
    items.append({
        'source': 'whatsapp', 'online_id': None, 'online_partner': int(row['cliente_id']),
        'key': 'distri_cobro_efwa_%s_%s_%s' % (when.strftime('%Y%m%d%H%M'), row['cliente_id'],
                                               int(round(amount))),
        'amount': amount, 'date': when.date(),
        'memo': 'Efectivo · recibió %s · aviso WhatsApp %s · avisó %s' % (
            row['recibio'] or 'sin dato', when.strftime('%d/%m %H:%M'), row['remitente']),
    })
if len({i['key'] for i in items}) != len(items):
    abort('claves de carga repetidas')
items.sort(key=lambda i: (i['date'], i['key']))
for item in items:
    item['partner'] = lab_partner(item['online_partner'])

posted_online = {}
for pay in OnlinePayment.search([('state', 'in', ONLINE_POSTED), ('amount', '>', 0)]):
    posted_online.setdefault(pay.online_partner_id, []).append(pay)
sheets = [i for i in items if i['source'] == 'planilla']


def problem(item):
    if item['online_id']:
        mirror = OnlinePayment.search([('online_id', '=', item['online_id'])])
        if mirror and mirror.state in ONLINE_POSTED:
            return 'ya está en Online como %s (%s)' % (mirror.name or mirror.move_name, mirror.state)
    for pay in posted_online.get(item['online_partner'], []):
        if pay.online_id != item['online_id'] and pay.date and \
                abs(pay.amount - item['amount']) <= AMOUNT_TOLERANCE and \
                item['date'] - BEFORE <= pay.date <= item['date'] + AFTER:
            return 'posible doble con %s del %s en Online' % (pay.name or pay.move_name, pay.date)
    if item['source'] == 'whatsapp':
        for sheet in sheets:
            if sheet['online_partner'] == item['online_partner'] and \
                    abs(sheet['amount'] - item['amount']) <= AMOUNT_TOLERANCE and \
                    item['date'] - BEFORE <= sheet['date'] <= item['date'] + AFTER:
                return 'el mismo efectivo que la planilla %s' % sheet['key']
    return ''


# ------------------------------------------------------------------ load
payments_before = Payment.search_count([])
amount_before = sum(Payment.search([]).mapped('amount'))
balances_before = {p.id: p._distri_account_balance() for p in partners.values()}
Route = env['distri.route'].sudo()
stats = {'planilla': {}, 'whatsapp': {}}
loaded_amount = 0.0
loaded_by_partner = {}
new_payments = Payment.browse()


def count(source, key):
    stats[source][key] = stats[source].get(key, 0) + 1


for item in items:
    if Payment.search_count([('payment_reference', '=', item['key'])]):
        count(item['source'], 'ya_cargado')
        continue
    reason = problem(item)
    if reason:
        count(item['source'], 'no_cargado')
        skip(clave=item['key'], origen=item['source'], motivo=reason)
        continue
    partner = item['partner']
    payment = Payment.create({
        'payment_type': 'inbound',
        'partner_type': 'customer',
        'partner_id': partner.id,
        'amount': item['amount'],
        'journal_id': journal.id,
        'date': item['date'],
        'memo': item['memo'],
        'payment_reference': item['key'],
        'company_id': env.company.id,
    })
    payment.action_post()
    Route._apply_oldest_first(payment, partner)
    if payment.move_id.state != 'posted':
        abort('el pago %s no tiene asiento' % item['key'])
    receivable = payment.move_id.line_ids.filtered(
        lambda l: l.account_id.account_type == 'asset_receivable')
    key = clave_documento(payment.move_id.name, sum(receivable.mapped('debit')),
                          sum(receivable.mapped('credit')))
    if Mov.search([('partner_id', '=', partner.id)]).filtered(
            lambda m: clave_documento(m.comprobante, m.debe, m.haber) == key):
        abort('el asiento %s coincide con un movimiento de la historia de Online: el saldo no '
              'lo contaría' % payment.move_id.name)
    new_payments |= payment
    loaded_amount += item['amount']
    loaded_by_partner[partner.id] = loaded_by_partner.get(partner.id, 0.0) + item['amount']
    count(item['source'], 'cargado')
    log(accion='efectivo', origen=item['source'], clave=item['key'], pago=payment.id,
        asiento=payment.move_id.id, nombre=payment.name, cliente=partner.id,
        cliente_online=item['online_partner'], importe=item['amount'], fecha=item['date'])

# ------------------------------------------------------------------ guards
env.flush_all()
created = sum(s.get('cargado', 0) for s in stats.values())
payments_after = Payment.search_count([])
if payments_after - payments_before != created or len(new_payments) != created:
    abort('pagos nuevos %s, filas cargadas %s' % (payments_after - payments_before, created))
amount_after = sum(Payment.search([]).mapped('amount'))
if abs((amount_after - amount_before) - loaded_amount) > 0.005:
    abort('el total de los pagos nuevos no es el total cargado')
for payment in new_payments:
    if payment.journal_id != journal or payment.payment_type != 'inbound':
        abort('el pago %s no quedó como cobro en efectivo' % payment.id)
for partner_id, amount in loaded_by_partner.items():
    moved = balances_before[partner_id] - Partner.browse(partner_id)._distri_account_balance()
    if abs(moved - amount) > 0.01:
        abort('el saldo del cliente %s bajó %s y no %s' % (partner_id, moved, amount))

print('RESULT', json.dumps({
    'diario': journal.code, 'planillas_leidas': len(sheets), 'avisos_chat_leidos': len(chat),
    'planilla': stats['planilla'], 'whatsapp': stats['whatsapp'],
    'pagos_antes': payments_before, 'pagos_despues': payments_after,
    'importe_cargado': round(loaded_amount, 2), 'clientes': len(loaded_by_partner),
    'estados_de_pago': sorted(set(new_payments.mapped('state'))),
}, ensure_ascii=False))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
