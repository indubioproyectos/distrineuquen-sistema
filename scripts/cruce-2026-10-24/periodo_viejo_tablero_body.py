# Step 13 of the 24/10/2026 cross (odoo shell body, run by paso13-periodo-viejo-tablero.sh).
#
# There is no time cutoff: the board gets every Mercado Pago income since
# Online started (01/04/2025), so each receipt of the chats can meet its
# transfer and every account can be checked back to its opening balance.
# Steps 3 and 5 loaded 18/03/2026 onwards and ten old operations; this one
# loads the rest of 01/04/2025-17/03/2026 from the ORIGINAL reports through
# the module's own importer (wholesale rules, refunds, proposals), with ONE
# change: the module's link to Online by amount alone is switched off for
# the import (it took drafts and non-unique candidates, steps 8 and 10), and
# the cross's own decisions are applied instead (paso13_viejo.csv, from the
# general audit of 25/10/2026):
# - online: closed as «Ya cargado en Online», naming the Online payment and
#   how the cross tied it; linked to that payment when it is free;
# - revendedor: set aside with the reseller the manager named (bank payers
#   have no CUIT, so no rule catches them on import);
# - saldo_inicial: before the opening balances of 10/04/2025, already inside
#   each customer's opening invoice: closed with that reason;
# - propia: transfers from the company's own account: closed as own;
# - empleado and pendiente: stay pending, with a note saying what they are.
# The collector Italo's CUIT learned as customer 33's identity is removed
# first. Every card touched gets a chatter note «Período viejo en el tablero
# (25/10/2026): …» (once: the idempotency mark). Nothing is confirmed.
# Guards (ABORT, nothing written): an operation of the file missing from the
# board after the import, an amount different from the file, any payment
# created or any customer balance moved.
# Rolls back unless DO_COMMIT=1.
import csv
import json
import os
from collections import Counter
from datetime import date

from odoo.addons.distri_pedido.models.cobros import (
    AMOUNT_TOLERANCE, REVERSAL_TYPES, digits, local_day, parse_mp_datetime, read_report,
    report_generated_at)
from odoo.addons.distri_pedido.models.mayoristas import APARTADO

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IN = os.environ.get('IN_DIR', '/cruce')
SINCE, UNTIL = date(2025, 4, 1), date(2026, 3, 17)
MARK = 'Período viejo en el tablero (25/10/2026)'
OPENING = date(2025, 4, 10)

Collection = env['distri.mp.collection'].sudo()
Report = env['distri.mp.report'].sudo()
Partner = env['res.partner'].sudo().with_context(active_test=False)
OnlinePayment = env['distri.online.payment'].sudo()
Attachment = env['ir.attachment'].sudo()
Payment = env['account.payment'].sudo()
Message = env['mail.message'].sudo()


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


def log(**data):
    print('LOG', json.dumps(dict(paso=13, **data), ensure_ascii=False, default=str))


def noted(record):
    return bool(Message.search_count([('model', '=', record._name), ('res_id', '=', record.id),
                                      ('body', 'ilike', MARK)]))


def note(record, text):
    if not noted(record):
        record.message_post(body='%s: %s' % (MARK, text))


def in_range(row):
    day = local_day(parse_mp_datetime(row.get('TRANSACTION_DATE')))
    return bool(day) and SINCE <= day <= UNTIL


def mirror_of(move_name):
    found = OnlinePayment.search(['|', ('name', '=', move_name), '|', ('move_name', '=', move_name),
                                  ('move_name', '=like', move_name + ' (%')])
    return found if len(found) == 1 else OnlinePayment.browse()


with open(os.path.join(IN, 'paso13_viejo.csv'), encoding='utf-8') as handle:
    plan = {r['op']: r for r in csv.DictReader(handle)}
if len(plan) != 6248:
    abort('paso13_viejo.csv no tiene las 6.248 operaciones del período viejo')

# an employee's CUIT is never a customer's identity: the collector Italo's was
# learned on 29/09 for customer 33 and would propose 33 for his transfers
Identity = env['distri.payer.identity'].sudo()
for wrong in Identity.search([('kind', '=', 'cuit'), ('value', '=', '23353107569')]):
    wrong.partner_id.commercial_partner_id.message_post(body=(
        '%s: se quita el CUIT 23353107569 de los pagadores conocidos: es de un cobrador de la empresa.' % MARK))
    wrong.unlink()

payments_before = Payment.search_count([])
all_partners = Partner.search([('ref', '=like', 'ODOO-%'), ('active', '=', True)]).filtered(
    lambda p: p == p.commercial_partner_id)
balances_before = {p.id: p._distri_account_balance() for p in all_partners}

# ------------------------------------------------------------------ 1. import
link_all = type(OnlinePayment)._distri_link_all
type(OnlinePayment)._distri_link_all = lambda self: None   # no link by amount alone
try:
    files = sorted(f for f in os.listdir(os.path.join(IN, 'mp')) if f.endswith('.xlsx'))
    reversals, imported = [], Counter()
    for name in files:
        data = open(os.path.join(IN, 'mp', name), 'rb').read()
        rows = [r for r in read_report(data) if in_range(r) and digits(r.get('SOURCE_ID')) in plan]
        reversals += [(digits(r.get('SOURCE_ID')), (r.get('TRANSACTION_TYPE') or '').upper(),
                       float((r.get('TRANSACTION_AMOUNT') or '0').replace(',', '.') or 0))
                      for r in rows if (r.get('TRANSACTION_TYPE') or '').upper() in REVERSAL_TYPES]
        on_board = set(Collection.search([('source_id', 'in', [digits(r.get('SOURCE_ID')) for r in rows])]).mapped('source_id'))
        rows = [r for r in rows if digits(r.get('SOURCE_ID')) not in on_board]
        if not rows:
            continue
        report = Report.create({'name': name + ' · período viejo', 'source': 'manual', 'company_id': env.company.id,
                                'generated_at': report_generated_at(name)})
        report.attachment_id = Attachment.create({
            'name': name, 'raw': data, 'res_model': report._name, 'res_id': report.id,
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'})
        report._load_rows(rows)
        imported['reportes'] += 1
        imported['filas'] += len(rows)
        log(accion='reporte', reporte=report.id, archivo=name, filas=len(rows))
    for source, kind, amount in reversals:
        if source:
            Collection.search([('source_id', '=', source)])._flag_reversal(kind, amount)
finally:
    type(OnlinePayment)._distri_link_all = link_all
env.flush_all()

board = {c.source_id: c for c in Collection.search([('kind', '=', 'income'), ('source_id', 'in', list(plan))])}
missing = set(plan) - set(board)
if missing:
    abort('%s operaciones del período viejo no quedaron en el tablero: %s' % (len(missing), sorted(missing)[:10]))
for op, row in plan.items():
    if abs(board[op].amount - float(row['importe'])) > 0.005:
        abort('la operación %s tiene otro importe en el tablero' % op)

# ------------------------------------------------------------------ 2. decisions
stats = Counter()
for op, row in plan.items():
    card = board[op]
    action = row['accion']
    if card.state not in ('draft', APARTADO) or card.payment_id:
        stats['otro_estado:%s:%s' % (action, card.state)] += 1
        continue
    if action == 'online':
        moves = [m for m in row['online'].split('|') if m]
        how = row['detalle'] or 'cruce'
        mirror = mirror_of(moves[0]) if len(moves) == 1 else OnlinePayment.browse()
        vals = {'state': 'ignored', 'ignore_reason': 'online',
                'ignore_note': ('Ya en Online según el cruce: %s (%s)' % (', '.join(moves), how))[:250]}
        if mirror and not mirror.collection_id and abs(mirror.amount - card.amount) <= AMOUNT_TOLERANCE:
            vals['online_payment_id'] = mirror.id
            mirror.write({'collection_id': card.id})
            stats['online_vinculado'] += 1
        else:
            vals['online_unlinked'] = True   # the module must not link it by amount later
        if card.state == APARTADO:
            card._distri_bring_back('cruce del período viejo')
        card._close_alerts('Ya en Online (cruce del período viejo)')
        card.write(vals)
        note(card, 'ya registrado en Online como %s; el cruce lo unió por %s.' % (', '.join(moves), how))
        stats['online'] += 1
    elif action == 'revendedor':
        if card.state == 'draft':
            card._distri_set_aside(label=row['detalle'])
        note(card, 'transferencia de %s, revendedor o distribuidora según la gerencia: su saldo lo lleva la '
                   'gerencia y no se imputa a un cliente directo.' % row['detalle'])
        stats['revendedor'] += 1
    elif action == 'saldo_inicial':
        if card.state == APARTADO:
            card._distri_bring_back('cruce del período viejo')
        card._close_alerts('Incluido en el saldo inicial')
        card.write({'state': 'ignored', 'ignore_reason': 'other',
                    'ignore_note': 'Anterior al saldo inicial del 10/04/2025: incluido en la factura de saldo inicial'})
        note(card, 'transferencia anterior al 10/04/2025: los saldos iniciales de Online ya la incluyen.')
        stats['saldo_inicial'] += 1
    elif action == 'propia':
        if card.state == 'draft':
            card._close_alerts('Movimiento propio')
            card.write({'state': 'ignored', 'ignore_reason': 'own',
                        'ignore_note': 'Transferencia desde una cuenta propia (cruce del 25/10/2026)'})
        note(card, 'transferencia desde una cuenta propia de la empresa o de la gerencia (%s).' % (row['detalle'] or '-'))
        stats['propia'] += 1
    elif action == 'empleado':
        note(card, 'transferencia de un empleado (%s): puede ser una rendición de efectivo, mercadería que se '
                   'llevó sin factura o el pago de un cliente. Queda pendiente para revisar.' % row['detalle'])
        stats['empleado'] += 1
    else:
        note(card, '%s. Queda pendiente para revisar.' % row['detalle'])
        stats['pendiente:%s' % card.state] += 1

# ------------------------------------------------------------------ guards
env.flush_all()
if Payment.search_count([]) != payments_before:
    abort('se crearon pagos')
for partner in all_partners:
    if abs(partner._distri_account_balance() - balances_before[partner.id]) > 0.01:
        abort('cambió el saldo del cliente %s' % partner.ref)
states = Counter(board[op].state for op in plan)
print('RESULT', json.dumps({**dict(imported), **dict(stats), 'estados_viejo': dict(states),
                            'tablero': dict(Counter(Collection.search([('kind', '=', 'income')]).mapped('state')))},
                           ensure_ascii=False))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
