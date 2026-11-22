# Cash of the paper sheets of 22-27/10/2026 (odoo shell body, run by
# cargar-efectivo.sh): the rows ale reviewed on 28/10 against the photos
# (planillas Jorge/Simon 22, 23, 26 and 27/10 and the second Simon 22/10
# sheet), read from /in/filas.jsonl. Same channel as step 4 of the 24/10
# cross (scripts/cruce-2026-10-24/cargar_efectivo_body.py): one inbound
# customer payment in «Efectivo» (CSH1), posted, date = sheet day, memo
# «Efectivo · cobró <REPARTIDOR> · planilla dd/mm · <factura | sin factura>»,
# payment_reference distri_cobro_ef_<AAAAMMDD>_<Online id>_<factura digits |
# fila<n>> (idempotent: a key already in the lab is never created again).
# Not loaded (SKIP): the same invoice twice in the input; a payment of the
# customer of the same amount from 3 days before to 10 after, in the lab (any
# journal, not cancelled), in Online (fresh read /in/pagos.json, any state but
# cancelled, and the mirror) or in the Online history; a cash payment (lab or
# Online) that already cites the invoice.
# Guards (ABORT, nothing written): a customer not found or not the expected
# one, payments created != rows loaded, totals or balances that do not move
# by exactly the loaded amounts.
# An optional «nota» in the row is appended to the memo (cross of the 2026
# cash sheets, 31/10/2026). Rolls back unless DO_COMMIT=1.
# How a payment is applied (22/11/2026, never oldest first: on 16/11 two
# payments of Online invoices were reconciled with unrelated lab invoices):
# the row's `factura` is resolved for the customer (commercial partner) among
# the posted lab out_invoices (account.move.name) and the Online history
# invoices (distri.cuenta.movimiento tipo factura, comprobante). A lab invoice
# is reconciled with that invoice only, up to its residual (any surplus stays
# open on the payment). An Online invoice is never reconciled: the payment's
# distri_history_invoice_id links it to the history row (as /cobro does,
# models/cobro_efectivo.py). Lab and Online share numbers (L4): with both,
# the one whose amount is importe_factura wins; still ambiguous, or not
# found, or no factura: WARN and the payment stays unreconciled.
# Cash sheets (19.0.1.75.0): each payment created is also put on its sheet
# row (distri.cash.sheet, models/cash_sheet.py _link_loader_row: collector +
# date + sheet index of the fila), in the same transaction, so a rollback
# drops both. Without that model installed the load runs as before.
import json
import os
import re
from datetime import date, timedelta

from odoo.addons.distri_pedido.models.cobros import AMOUNT_TOLERANCE
from odoo.addons.distri_pedido.models.cuenta import clave_documento
from odoo.addons.distri_pedido.models.puesta_al_dia import ONLINE_POSTED

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IN = os.environ.get('IN_DIR', '/in')
BEFORE, AFTER = timedelta(days=3), timedelta(days=10)

Partner = env['res.partner'].sudo().with_context(active_test=False)
Payment = env['account.payment'].sudo()
Move = env['account.move'].sudo()
CashCollection = env['distri.cash.collection'].sudo()
OnlinePayment = env['distri.online.payment'].sudo()
Mov = env['distri.cuenta.movimiento'].sudo()
CashSheet = env['distri.cash.sheet'].sudo() if 'distri.cash.sheet' in env.registry else None


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


def out(tag, **data):
    print(tag, json.dumps(data, ensure_ascii=False, default=str))


journal = env['account.journal'].sudo().search([('code', '=', 'CSH1'), ('type', '=', 'cash'),
                                                ('company_id', '=', env.company.id)])
if len(journal) != 1:
    abort('no hay un único diario Efectivo (CSH1)')

with open(os.path.join(IN, 'filas.jsonl'), encoding='utf-8') as handle:
    items = [json.loads(line) for line in handle if line.strip()]
with open(os.path.join(IN, 'pagos.json'), encoding='utf-8') as handle:
    online_fresh = [p for p in json.load(handle) if p['payment_type'] == 'inbound'
                    and p['state'] not in ('cancel', 'canceled') and p['partner_id']]
online_by_partner = {}
for p in online_fresh:
    online_by_partner.setdefault(p['partner_id'][0], []).append(p)

for item in items:
    found = Partner.search([('ref', '=', 'ODOO-%s' % item['online_partner'])])
    partner = found.commercial_partner_id
    if len(partner) != 1 or partner.id != item['lab_partner'] or not partner.active \
            or partner.distri_generic_partner:
        abort('cliente de Online %s (fila %s) no es el esperado en el lab' % (item['online_partner'], item['fila']))
    item['partner'] = partner
    item['date'] = date.fromisoformat(item['fecha'])
items.sort(key=lambda i: (i['date'], i['repartidor'], i['fila']))


def near(day, other, amount, other_amount):
    return other and abs(other_amount - amount) <= AMOUNT_TOLERANCE and day - BEFORE <= other <= day + AFTER


def problem(item):
    partner, day, amount, fact = item['partner'], item['date'], item['efectivo'], item['factura']
    lab = Payment.search([('partner_id', 'child_of', partner.id), ('payment_type', '=', 'inbound'),
                          ('state', '!=', 'canceled')])
    for pay in lab:
        if near(day, pay.date, amount, pay.amount):
            return 'en el lab ya está %s del %s por %s (%s)' % (pay.name, pay.date, pay.amount, pay.journal_id.code)
        # Opt-in per row: a confirmed second partial cash payment of the same invoice.
        if fact and pay.journal_id.type == 'cash' and fact in (pay.memo or '') \
                and not item.get('permitir_factura_repetida'):
            return 'en el lab %s (%s) ya cita la factura %s' % (pay.name, pay.journal_id.code, fact)
    for pay in online_by_partner.get(item['online_partner'], []):
        when = date.fromisoformat(pay['date'])
        if near(day, when, amount, pay['amount']):
            return 'en Online ya está %s del %s por %s (%s, %s)' % (pay['name'], when, pay['amount'],
                                                                   pay['journal_id'][1], pay['state'])
        text = '%s %s' % (pay['memo'] or '', pay['payment_reference'] or '')
        if fact and fact in text and 'EFECTIVO' in (pay['journal_id'][1] or '').upper():
            return 'en Online %s (%s) ya cita la factura %s' % (pay['name'], pay['state'], fact)
    for pay in OnlinePayment.search([('online_partner_id', '=', item['online_partner']),
                                     ('state', 'in', ONLINE_POSTED)]):
        if near(day, pay.date, amount, pay.amount):
            return 'en la copia de Online ya está %s del %s' % (pay.name or pay.move_name, pay.date)
    for mov in Mov.search([('partner_id', 'child_of', partner.id), ('tipo', '=', 'pago')]):
        if near(day, mov.fecha, amount, (mov.haber or 0) - (mov.debe or 0)):
            return 'en la historia de Online ya está %s del %s' % (mov.comprobante, mov.fecha)
    return ''


def resolve_invoice(item):
    """('lab', account.move) | ('online', distri.cuenta.movimiento) |
    ('none', reason) | ('ambiguous', reason) for the row's `factura`."""
    partner, fact = item['partner'], item['factura']
    if not fact:
        return 'none', 'la fila no trae factura'
    labs = Move.search([('move_type', '=', 'out_invoice'), ('state', '=', 'posted'), ('name', '=', fact),
                        ('commercial_partner_id', '=', partner.id)])
    onlines = Mov.search([('tipo', '=', 'factura'), ('comprobante', '=', fact),
                          ('partner_id', 'child_of', partner.id)])
    candidates = [('lab', m, m.amount_total) for m in labs] + \
                 [('online', m, (m.debe or 0) - (m.haber or 0)) for m in onlines]
    if not candidates:
        return 'none', 'la factura %s no está en el lab ni en la historia de Online del cliente' % fact
    if len(candidates) > 1:
        wanted = item.get('importe_factura')
        matching = [c for c in candidates if wanted and abs(c[2] - wanted) <= AMOUNT_TOLERANCE]
        if len(matching) != 1:
            return 'ambiguous', 'la factura %s es ambigua (%s) para importe_factura %s' % (
                fact, ', '.join('%s %s por %s' % (k, r.id, a) for k, r, a in candidates), wanted)
        candidates = matching
    return candidates[0][0], candidates[0][1]


# ------------------------------------------------------------------ load
payments_before = Payment.search_count([])
amount_before = sum(Payment.search([]).mapped('amount'))
partners = {i['partner'].id: i['partner'] for i in items}
balances_before = {pid: p._distri_account_balance() for pid, p in partners.items()}
seen_invoices = {}
loaded = Payment.browse()
loaded_by_partner = {}
stats = {}
for item in items:
    partner = item['partner']
    if Payment.search_count([('payment_reference', '=', item['clave'])]):
        stats['ya_cargado'] = stats.get('ya_cargado', 0) + 1
        out('SKIP', fila=item['fila'], clave=item['clave'], motivo='ya cargado (misma clave)')
        continue
    if item['factura'] and item['factura'] in seen_invoices:
        stats['no_cargado'] = stats.get('no_cargado', 0) + 1
        out('SKIP', fila=item['fila'], motivo='la factura %s ya va en la fila %s' % (
            item['factura'], seen_invoices[item['factura']]))
        continue
    reason = problem(item)
    if reason:
        stats['no_cargado'] = stats.get('no_cargado', 0) + 1
        out('SKIP', fila=item['fila'], cliente=partner.name, importe=item['efectivo'], fecha=item['fecha'],
            motivo=reason)
        continue
    if item['factura']:
        seen_invoices[item['factura']] = item['fila']
    doc = item['factura'] or 'sin factura'
    kind, target = resolve_invoice(item)
    payment = Payment.create({
        'payment_type': 'inbound', 'partner_type': 'customer', 'partner_id': partner.id,
        'amount': item['efectivo'], 'journal_id': journal.id, 'date': item['date'],
        'memo': 'Efectivo · cobró %s · planilla %s · %s%s' % (item['repartidor'], item['date'].strftime('%d/%m'), doc,
                                                          ' · %s' % item['nota'] if item.get('nota') else ''),
        'payment_reference': item['clave'], 'company_id': env.company.id,
        'distri_history_invoice_id': target.id if kind == 'online' else False,
    })
    payment.action_post()
    if kind == 'lab':
        # only the named invoice, up to its residual; never another lab invoice
        CashCollection._cash_reconcile(payment, target)
        reconciled = payment.move_id.line_ids.matched_debit_ids.debit_move_id.move_id
        if target in reconciled:
            applied = 'concilia %s' % target.name
        else:
            applied = 'sin conciliar (la factura %s no tiene saldo)' % target.name
            out('WARN', fila=item['fila'], cliente=partner.name, motivo=applied)
        if reconciled - target:
            abort('el pago %s quedó conciliado con otra factura' % item['clave'])
    elif kind == 'online':
        applied = 'vincula factura Online %s' % target.comprobante
    else:
        applied = 'sin factura, sin conciliar' if kind == 'none' else 'factura ambigua, sin conciliar'
        out('WARN', fila=item['fila'], cliente=partner.name, motivo='%s: %s' % (applied, target))
    out('APLICA', fila=item['fila'], clave=item['clave'], aplicado=applied)
    if payment.move_id.state != 'posted':
        abort('el pago %s no tiene asiento' % item['clave'])
    receivable = payment.move_id.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
    key = clave_documento(payment.move_id.name, sum(receivable.mapped('debit')), sum(receivable.mapped('credit')))
    if Mov.search([('partner_id', '=', partner.id)]).filtered(
            lambda m: clave_documento(m.comprobante, m.debe, m.haber) == key):
        abort('el asiento %s coincide con un movimiento de la historia de Online' % payment.move_id.name)
    if CashSheet is not None:
        try:
            with env.cr.savepoint():
                if CashSheet._link_loader_row(item, payment).payment_id == payment:
                    stats['planilla_vinculadas'] = stats.get('planilla_vinculadas', 0) + 1
        except Exception as exc:  # the sheet row is a convenience: never stops the load
            out('WARN', fila=item['fila'], motivo='no quedó en la planilla: %s' % exc)
    loaded |= payment
    loaded_by_partner[partner.id] = loaded_by_partner.get(partner.id, 0.0) + item['efectivo']
    stats['cargado'] = stats.get('cargado', 0) + 1
    out('LOG', accion='efectivo', fila=item['fila'], clave=item['clave'], pago=payment.id, nombre=payment.name,
        cliente=partner.id, cliente_nombre=partner.name, importe=item['efectivo'], fecha=item['fecha'],
        repartidor=item['repartidor'], factura=doc, aplicado=applied, inferida=item['inferida'],
        importe_factura=item['importe_factura'], clasificacion=item['clasificacion'])
    if item['importe_factura'] and item['efectivo'] > item['importe_factura'] + AMOUNT_TOLERANCE:
        out('WARN', fila=item['fila'], cliente=partner.name, motivo='efectivo mayor que la factura',
            efectivo=item['efectivo'], factura=item['importe_factura'],
            excedente=round(item['efectivo'] - item['importe_factura'], 2))

# ------------------------------------------------------------------ guards
env.flush_all()
created = stats.get('cargado', 0)
payments_after = Payment.search_count([])
if payments_after - payments_before != created or len(loaded) != created:
    abort('pagos nuevos %s, filas cargadas %s' % (payments_after - payments_before, created))
loaded_amount = sum(loaded.mapped('amount'))
if abs((sum(Payment.search([]).mapped('amount')) - amount_before) - loaded_amount) > 0.005:
    abort('el total de los pagos nuevos no es el total cargado')
for payment in loaded:
    if payment.journal_id != journal or payment.payment_type != 'inbound' or payment.state == 'canceled':
        abort('el pago %s no quedó como cobro en efectivo' % payment.id)
for pid, amount in loaded_by_partner.items():
    after = Partner.browse(pid)._distri_account_balance()
    moved = balances_before[pid] - after
    if abs(moved - amount) > 0.01:
        abort('el saldo del cliente %s bajó %s y no %s' % (pid, moved, amount))
    if after < -AMOUNT_TOLERANCE:
        out('CREDITO', cliente=pid, cliente_nombre=Partner.browse(pid).name, saldo_antes=balances_before[pid],
            cargado=amount, saldo_despues=round(after, 2))
by_sheet = {}
for payment in loaded:
    sheet = re.search(r'cobró (\S+) · planilla (\d\d/\d\d)', payment.memo).groups()
    row = by_sheet.setdefault(' '.join(sheet), [0, 0.0])
    row[0] += 1
    row[1] += payment.amount
names = sorted(loaded.mapped('name'))
out('RESULT', filas=len(items), **stats, importe_cargado=round(loaded_amount, 2),
    por_planilla={k: [v[0], round(v[1], 2)] for k, v in sorted(by_sheet.items())},
    primero=names[0] if names else None, ultimo=names[-1] if names else None,
    ids=sorted(loaded.ids), estados=sorted(set(loaded.mapped('state'))))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
