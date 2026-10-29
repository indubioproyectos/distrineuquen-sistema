# Step 3 of the 24/10/2026 cross (odoo shell body, run by paso3-cargar-mp.sh).
#
# 1. Board: the ORIGINAL Mercado Pago reports go through the module's own
#    importer (distri.mp.report._import_file) limited to 18/03/2026-24/10/2026,
#    oldest file first. The importer, not hand-made cards, so every income gets
#    exactly what the board does with a report: one card per operation
#    (unique source_id, so the overlapping files count once), refunds flagged
#    on their transfer, the wholesale rules, the proposals, the daily closes
#    and the «Ya cargado en Online» links against the Online mirror. Refunds
#    are applied again after the last file, because a refund can sit in an
#    earlier file than its transfer.
# 2. carga_final.csv, column final:
#    - «cargar»: the lab customer (ref ODOO-<propuesta_id>), the receipt file
#      as an attachment of the card, a note with the receipt and the evidence,
#      and the module's «Confirmar» (payment in «Mercado Pago», oldest invoice
#      first). A card the module already took out of the board (linked to an
#      Online payment by amount, wholesale, refunded) is not confirmed: it is
#      listed (SKIP) for a person to decide.
#    - «ya en Online»: closed as «Ya cargado en Online» with the reason; linked
#      to the Online payment when the reason names it and the mirror has it free.
#    - «revisar»: stays pending, with the reason as a note.
#    Any other income stays on the board as the importer left it.
# Guards (ABORT, nothing written): a customer not found, an operation missing
# from the board, an amount different from the cross, a card confirmed for
# another customer, payments created != cards confirmed, totals or customer
# balances that do not move by exactly the confirmed amounts.
# Idempotent: a second run skips what is already done.
# Rolls back unless DO_COMMIT=1.
import csv
import json
import os
import re
from datetime import date

from odoo.addons.distri_pedido.models.cobros import (
    AMOUNT_TOLERANCE, REVERSAL_TYPES, digits, local_day, parse_mp_datetime, read_report,
    report_generated_at)
from odoo.addons.distri_pedido.models.cuenta import clave_documento

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
SIMULATE_CLEANUP = os.environ.get('SIMULAR_LIMPIEZA') == '1'
IN = os.environ.get('IN_DIR', '/cruce')
SINCE = date.fromisoformat(os.environ.get('CRUCE_DESDE') or '2026-03-18')
UNTIL = date.fromisoformat(os.environ.get('CRUCE_HASTA') or '2026-10-24')
CROSS_FILE = os.environ.get('CARGA_ARCHIVO') or 'carga_final.csv'
# only the operations of the cross file go to the board (old-period runs)
ONLY_CROSS_OPS = os.environ.get('SOLO_OPERACIONES_DEL_CRUCE') == '1'
RUN_NOTE = os.environ.get('NOTA_CORRIDA') or 'cruce verificado del 24/10/2026'
ONLINE_NAME_RE = re.compile(r'\bP[A-Z]+\d*/\d{4}/\d+\b')

Collection = env['distri.mp.collection'].sudo()
Report = env['distri.mp.report'].sudo()
Partner = env['res.partner'].sudo().with_context(active_test=False)
OnlinePayment = env['distri.online.payment'].sudo()
Attachment = env['ir.attachment'].sudo()
Payment = env['account.payment'].sudo()
Mov = env['distri.cuenta.movimiento'].sudo()


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


def log(**data):
    print('LOG', json.dumps(dict(paso=3, **data), ensure_ascii=False, default=str))


def skip(**data):
    print('SKIP', json.dumps(data, ensure_ascii=False, default=str))


def read_csv(name):
    with open(os.path.join(IN, name), encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def in_range(row):
    day = local_day(parse_mp_datetime(row.get('TRANSACTION_DATE')))
    return bool(day) and SINCE <= day <= UNTIL


if DO_COMMIT and SIMULATE_CLEANUP:
    abort('SIMULAR_LIMPIEZA es solo para probar')
if SIMULATE_CLEANUP:
    # step 1 in this same transaction, without its commit/rollback tail
    source = open(os.path.join(IN, 'limpiar_tablero_body.py'), encoding='utf-8').read()
    head, sep, tail = source.rpartition('\nif DO_COMMIT:')
    if not sep or 'env.cr.commit()' not in tail:
        abort('limpiar_tablero_body.py cambió: no se puede simular la limpieza')
    exec(compile(head, 'limpiar_tablero_body.py', 'exec'), {'env': env, 'os': os})

files = sorted(f for f in os.listdir(os.path.join(IN, 'mp')) if f.endswith('.xlsx'))
cross = read_csv(CROSS_FILE)
cross_ops = {r['mp_op'] for r in cross}
foreign = Report.search([('name', 'not in', files)])
if foreign:
    abort('el tablero tiene %s reportes que no son de este cruce: correr antes el paso 1'
          % len(foreign))

def import_rows(name, rows):
    """Report._import_file with the rows already chosen: the same report
    record and file attachment, then the module's own _load_rows."""
    raw = open(os.path.join(IN, 'mp', name), 'rb').read()
    report = Report.create({'name': name, 'source': 'manual', 'company_id': env.company.id,
                            'generated_at': report_generated_at(name)})
    report.attachment_id = Attachment.create({
        'name': name, 'raw': raw, 'res_model': report._name, 'res_id': report.id,
        'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'})
    report._load_rows(rows)
    return report


payments_before = Payment.search_count([])
amount_before = sum(Payment.search([]).mapped('amount'))

# ------------------------------------------------------------------ 1. board
imported, files_without_rows, files_already = [], 0, 0
reversals = []
for name in files:
    data = open(os.path.join(IN, 'mp', name), 'rb').read()
    rows = [r for r in read_report(data) if in_range(r)
            and (not ONLY_CROSS_OPS or digits(r.get('SOURCE_ID')) in cross_ops)]
    reversals += [(digits(r.get('SOURCE_ID')), (r.get('TRANSACTION_TYPE') or '').upper(),
                   float((r.get('TRANSACTION_AMOUNT') or '0').replace(',', '.') or 0))
                  for r in rows if (r.get('TRANSACTION_TYPE') or '').upper() in REVERSAL_TYPES]
    if not rows:
        files_without_rows += 1
        continue
    if not ONLY_CROSS_OPS and Report.search_count([('name', '=', name)]):
        files_already += 1
        continue
    if ONLY_CROSS_OPS:
        # an old-period run re-reads files already imported for the recent
        # period: only the selected operations not yet on the board
        on_board = set(Collection.search([('source_id', 'in', [digits(r.get('SOURCE_ID')) for r in rows])]).mapped('source_id'))
        rows = [r for r in rows if digits(r.get('SOURCE_ID')) not in on_board]
        if not rows:
            files_already += 1
            continue
        data = None
    report = (Report._import_file(name, data, 'manual', SINCE, UNTIL) if data is not None
              else import_rows(name, rows))
    imported.append({'reporte': report.id, 'filas': report.row_count,
                     'nuevas': report.new_count, 'repetidas': report.duplicate_count})
    log(accion='reporte', reporte=report.id, archivo=name, filas=report.row_count,
        nuevas=report.new_count)
for source, kind, amount in reversals:
    if source:
        Collection.search([('source_id', '=', source)])._flag_reversal(kind, amount)

env.flush_all()
board = {c.source_id: c for c in Collection.search([('kind', '=', 'income')])}
expected = {r['op_id'] for r in read_csv('mp_unificado.csv')
            if r['clase'].startswith('cobro') and SINCE.isoformat() <= r['fecha_ar'][:10] <= UNTIL.isoformat()
            and (not ONLY_CROSS_OPS or r['op_id'] in cross_ops)}
board_in_range = {s for s, c in board.items() if c.date_day and SINCE <= c.date_day <= UNTIL}
coverage = {'mp_unificado_ingresos': len(expected), 'tablero_ingresos': len(board_in_range),
            'faltan_en_tablero': len(expected - board_in_range),
            'sobran_en_tablero': len(board_in_range - expected)}
if expected - board_in_range:
    print('WARN', json.dumps({'faltan_en_tablero': sorted(expected - board_in_range)[:30]}))

# ------------------------------------------------------------------ 2. cross
receipts = {}
for row in read_csv('cruce_final.csv'):
    receipts.setdefault((row['archivo'], row['mp_op']), row)

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


cards = {}
for row in cross:
    card = board.get(row['mp_op'])
    if not card:
        abort('la operación %s (%s) no está en el tablero' % (row['mp_op'], row['final']))
    if abs(card.amount - float(row['importe'])) > 0.005:
        abort('la operación %s tiene otro importe en el tablero' % row['mp_op'])
    cards[row['mp_op']] = card
to_load = [r for r in cross if r['final'] == 'cargar']
for row in to_load:
    lab_partner(int(row['propuesta_id']))
balances_before = {p.id: p._distri_account_balance() for p in partners.values()}


def noted(card):
    return bool(env['mail.message'].sudo().search_count([
        ('model', '=', card._name), ('res_id', '=', card.id), ('body', 'ilike', RUN_NOTE)]))


def attach(card, row):
    """The receipt file on the card (once). Returns the attachment or None."""
    name = row['comprobante']
    if not name:
        return None
    info = receipts.get((name, row['mp_op']))
    path = os.path.join(IN, 'img', '%s__%s' % (info['export'], name)) if info else ''
    if not info or not os.path.exists(path):
        abort('falta el archivo del comprobante de la operación %s' % row['mp_op'])
    old = Attachment.search([('res_model', '=', card._name), ('res_id', '=', card.id),
                             ('name', '=', name)], limit=1)
    if old:
        return old
    return Attachment.create({'name': name, 'raw': open(path, 'rb').read(),
                              'res_model': card._name, 'res_id': card.id})


def receipt_text(row):
    info = receipts.get((row['comprobante'], row['mp_op'])) if row['comprobante'] else None
    if not info:
        return 'Sin comprobante por WhatsApp.'
    parts = ['Comprobante %s' % info['archivo'],
             'export del %s' % ('gerente' if info['export'] == 'gerente' else 'administrador'),
             'llegó el %s' % info['llegada'].replace('T', ' ')[:16]]
    if info['remitente']:
        parts.append('lo mandó %s' % info['remitente'])
    if info['nivel']:
        parts.append('vínculo %s' % info['nivel'])
    if info['leyenda']:
        parts.append('leyenda «%s»' % info['leyenda'][:120])
    return ', '.join(parts) + '.'


def post_note(card, body, attachment):
    if noted(card):
        return False
    card.message_post(body=body, attachment_ids=[attachment.id] if attachment else [])
    return True


stats = {'cargar': {}, 'ya_en_online': {}, 'revisar': {}}


def count(group, key):
    stats[group][key] = stats[group].get(key, 0) + 1


# cards the module closed as «Ya en Online» only by amount, against a reviewed
# verdict naming them (REABRIR_OPS, comma separated): back to draft before loading
for op in filter(None, (os.environ.get('REABRIR_OPS') or '').split(',')):
    card = cards.get(op)
    if not card or card.state != 'ignored' or card.ignore_reason != 'online' or card.payment_id:
        abort('la operación %s no está cerrada como Ya en Online: no se reabre' % op)
    pay = card.online_payment_id
    if pay:
        pay.write({'collection_id': False, 'link_kind': False})
    card.write({'online_payment_id': False, 'online_unlinked': True, 'online_conflict': False,
                'state': 'draft', 'ignore_reason': False, 'ignore_note': False})
    # this note must not contain RUN_NOTE: noted() would then skip the load note
    card.message_post(body='Vuelve a borrador para cargarlo: el sistema lo había unido solo por '
                           'importe a %s, que corresponde a otro cobro.' % (
                               pay and (pay.name or pay.move_name) or 'un pago de Online'))
    log(accion='reabierta', op=op, cobro=card.id, pago_online=pay.id if pay else None)

confirmed_amount = 0.0
confirmed_by_partner = {}
new_payments = Payment.browse()
for row in to_load:
    card = cards[row['mp_op']]
    partner = lab_partner(int(row['propuesta_id']))
    if card.state == 'confirmed':
        if card.payment_id and card.partner_id.commercial_partner_id == partner:
            count('cargar', 'ya_confirmado')
            continue
        abort('la operación %s ya está confirmada para otro cliente' % row['mp_op'])
    reason = ''
    if card.state != 'draft':
        # only ids and Online payment numbers: never customer names
        online = card.online_payment_id
        reason = 'estado %s%s' % (card.state, (' (%s%s)' % (card.ignore_reason or '', (
            ': %s del %s, %s en Online' % (online.name or online.move_name or 'pago %s' % online.online_id,
                                          online.date, online.state)) if online else ''))
            if card.state == 'ignored' else '')
    elif card.reversal:
        reason = card.reversal
    if reason:
        count('cargar', 'no_confirmado')
        skip(op=row['mp_op'], cobro=card.id, final='cargar', motivo=reason)
        continue
    attachment = attach(card, row)
    card.write({'partner_id': partner.id, 'proposal': 'manual',
                'proposal_note': ('Cruce 24/10: %s' % (row['por'] or row['motivo_final']))[:250]})
    post_note(card, 'Cobro cargado por el %s. Cómo se identificó al cliente: %s. %s' % (
        RUN_NOTE, row['motivo_final'] or row['por'] or 'sin detalle', receipt_text(row)), attachment)
    card.action_confirm()
    payment = card.payment_id
    if not payment or card.state != 'confirmed':
        abort('la operación %s no quedó confirmada' % row['mp_op'])
    new_payments |= payment
    confirmed_amount += card.amount
    confirmed_by_partner[partner.id] = confirmed_by_partner.get(partner.id, 0.0) + card.amount
    count('cargar', 'confirmado')
    log(accion='confirmado', op=row['mp_op'], cobro=card.id, pago=payment.id, asiento=payment.move_id.id,
        cliente=partner.id, cliente_online=int(row['propuesta_id']), importe=card.amount,
        adjunto=attachment.id if attachment else None)

for row in (r for r in cross if r['final'] == 'ya en Online'):
    card = cards[row['mp_op']]
    attachment = attach(card, row)
    if card.state == 'confirmed':
        abort('la operación %s figura ya en Online pero está confirmada en el lab' % row['mp_op'])
    if card.state == 'draft':
        pay = OnlinePayment.browse()
        for name in ONLINE_NAME_RE.findall(row['motivo_final']):
            found = OnlinePayment.search(['|', ('name', '=', name), ('move_name', '=', name)])
            if len(found) == 1 and not found.collection_id and \
                    abs(found.amount - card.amount) <= AMOUNT_TOLERANCE:
                pay = found
                break
        if pay:
            pay.write({'collection_id': card.id, 'link_kind': 'amount'})
            card._distri_mark_online(pay)
            count('ya_en_online', 'vinculado_a_pago_online')
        else:
            card.write({'state': 'ignored', 'ignore_reason': 'online',
                        'ignore_note': ('Ya cargado en Online (%s): %s' % (
                            RUN_NOTE, row['motivo_final'] or 'sin detalle'))[:250]})
            count('ya_en_online', 'cerrado')
        if row['propuesta_id'] and not card.partner_id:
            card.partner_id = lab_partner(int(row['propuesta_id']))
        log(accion='ya_en_online', op=row['mp_op'], cobro=card.id, pago_online=pay.id or None,
            adjunto=attachment.id if attachment else None)
    elif card.state == 'ignored' and card.ignore_reason == 'online':
        count('ya_en_online', 'ya_estaba_en_online')
    else:
        count('ya_en_online', 'otro_estado')
        skip(op=row['mp_op'], cobro=card.id, final='ya en Online', motivo='estado %s' % card.state)
    post_note(card, 'Ya cargado en Online según el %s: %s. %s' % (
        RUN_NOTE, row['motivo_final'] or 'sin detalle', receipt_text(row)), attachment)

for row in (r for r in cross if r['final'] == 'revisar'):
    card = cards[row['mp_op']]
    attachment = attach(card, row)
    written = post_note(card, 'Queda para revisar (%s): %s. %s' % (
        RUN_NOTE, row['motivo_final'] or 'sin detalle', receipt_text(row)), attachment)
    count('revisar', '%s:%s' % (card.state, 'nota' if written else 'ya_tenia_nota'))

# ------------------------------------------------------------------ guards
env.flush_all()
payments_after = Payment.search_count([])
if payments_after - payments_before != stats['cargar'].get('confirmado', 0):
    abort('pagos nuevos %s, cobros confirmados %s' % (
        payments_after - payments_before, stats['cargar'].get('confirmado', 0)))
amount_after = sum(Payment.search([]).mapped('amount'))
if abs((amount_after - amount_before) - confirmed_amount) > 0.005 or \
        abs(sum(new_payments.mapped('amount')) - confirmed_amount) > 0.005:
    abort('el total de los pagos nuevos no es el total confirmado')
mp_journal = env['account.journal']._distri_mp_journal()
for payment in new_payments:
    if payment.journal_id != mp_journal or payment.payment_type != 'inbound' or \
            payment.state not in ('in_process', 'paid') or payment.move_id.state != 'posted':
        abort('el pago %s no quedó como cobro de Mercado Pago asentado' % payment.id)
    receivable = payment.move_id.line_ids.filtered(
        lambda l: l.account_id.account_type == 'asset_receivable')
    key = clave_documento(payment.move_id.name, sum(receivable.mapped('debit')),
                          sum(receivable.mapped('credit')))
    twins = Mov.search([('partner_id', '=', payment.partner_id.id)]).filtered(
        lambda m: clave_documento(m.comprobante, m.debe, m.haber) == key)
    if twins:
        abort('el asiento %s coincide con un movimiento de la historia de Online: el saldo no '
              'lo contaría' % payment.move_id.name)
for partner_id, amount in confirmed_by_partner.items():
    moved = balances_before[partner_id] - Partner.browse(partner_id)._distri_account_balance()
    if abs(moved - amount) > 0.01:
        abort('el saldo del cliente %s bajó %s y no %s' % (partner_id, moved, amount))

env.flush_all()
env.cr.execute("""select kind || ':' || state, count(*) from distri_mp_collection
                   group by 1 order by 1""")
print('RESULT', json.dumps({
    'simulada_limpieza': SIMULATE_CLEANUP,
    'archivos': len(files), 'importados': len(imported), 'sin_filas_en_el_periodo': files_without_rows,
    'ya_importados': files_already, 'cobertura': coverage,
    'cargar': stats['cargar'], 'ya_en_online': stats['ya_en_online'], 'revisar': stats['revisar'],
    'pagos_antes': payments_before, 'pagos_despues': payments_after,
    'importe_confirmado': round(confirmed_amount, 2), 'clientes': len(confirmed_by_partner),
    'tablero': dict(env.cr.fetchall())}, ensure_ascii=False))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
