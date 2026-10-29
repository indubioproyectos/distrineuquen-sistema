# Update of 27/10/2026 (odoo shell body, run by cargar-actualizacion.sh).
#
# Brings the receipts of ale's export of the managers' group (from 23/10) to
# the migration board, verified against the Mercado Pago settlement of 27/10,
# with the module's own importers (nothing hand-made):
#   1. (dry run only) the fresh read of Online is applied to the payments
#      mirror in this same transaction, so «Ya en Online» is current. With
#      DO_COMMIT=1 the wrapper already ran the full scripts/online sync.
#   2. the MP report through distri.mp.report._import_file: one income card
#      per operation (an operation already on the board counts once), refunds
#      flagged, wholesale rules, links with the Online mirror. An MP movement
#      without receipt is «Falta el comprobante»: never on the migration board.
#   3. the chat through distri.whatsapp.chat.import._distri_import, oldest
#      message first (the module keeps the first copy of a receipt as the
#      original): one message per file hash (a photo already in the lab is
#      skipped), photo + text + sender + time, OCR, link to its MP movement
#      (operation number, else same amount within 2 min of the stamp read on
#      the photo, else the module's window, graded C and never approvable),
#      «Sin movimiento en MP» when the report covers its moment and none.
#   4. the report, newest receipt first: every file of the export, whether it
#      was already in the lab (same hash, or the same WhatsApp message of the
#      manager's export: same minute and amount), in Online, a duplicate, not
#      a receipt, a refund, a wholesaler's, or new with its grade and box.
# 3b. two photos of the same payment: the card keeps the one with the
#     operation number, the other is closed as a copy.
# 3c. the manager's cash notices since 15/09 (all exports): loaded in
#     «Efectivo» only when neither the lab nor Online has them.
# Guards (ABORT, nothing written): a payment other than that cash, a customer
# balance moving by anything but that cash, a card that existed before
# changing state.
# Rolls back unless DO_COMMIT=1. Idempotent (a second run finds the hashes).
import json
import os
import re
import hashlib
import unicodedata
from collections import Counter, defaultdict
from datetime import date, timedelta

from odoo.addons.distri_pedido.models.cobros import (
    AMOUNT_TOLERANCE, digits, local_when, parse_mp_datetime, read_report)
from odoo.addons.distri_pedido.models.puesta_al_dia import MEDIA_TYPES, _Source, parse_chat

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
SIM_ONLINE = os.environ.get('SIMULAR_ONLINE') == '1'
IN = os.environ.get('IN_DIR', '/in')
ZIP = os.environ['ZIP_NAME']
MP = os.environ['MP_NAME']
DESDE = date.fromisoformat(os.environ.get('DESDE') or '2026-10-23')
HASTA = date.fromisoformat(os.environ.get('HASTA') or '2026-10-27')

Partner = env['res.partner'].sudo().with_context(active_test=False)
Payment = env['account.payment'].sudo()
Collection = env['distri.mp.collection'].sudo()
Message = env['distri.whatsapp.message'].sudo()
Report = env['distri.mp.report'].sudo()
Chat = env['distri.whatsapp.chat.import'].sudo()


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


def out(tag, **data):
    print(tag, json.dumps(data, ensure_ascii=False, default=str))


# ------------------------------------------------------------ before
payments_before = Payment.search_count([])
all_partners = Partner.search([('ref', '=like', 'ODOO-%'), ('active', '=', True)]).filtered(
    lambda p: p == p.commercial_partner_id)
balances_before = {p.id: p._distri_account_balance() for p in all_partners}
cards_before = {c.id: c.state for c in Collection.search([])}
keys_before = set(Message.search([('wa_message_id', '=like', 'chat:%')]).mapped('wa_message_id'))
last_chat = max(Message.search([('wa_message_id', '=like', 'chat:%'), ('message_type', '!=', 'text')],
                               order='timestamp desc', limit=1).mapped('timestamp') or [None])

# ------------------------------------------------------------ 1. Online mirror (dry run)
if SIM_ONLINE:
    with open(os.path.join(IN, 'payload.json'), encoding='utf-8') as handle:
        payload = json.load(handle)
    links = env['distri.online.payment'].sudo()._distri_load(payload['payments'])
    out('RESULT', paso='online', leido=payload.get('read_at'), pagos=len(payload['payments']),
        clientes_nuevos_en_online=len(payload['newp']), filas_nuevas=len(payload['new']), **links)

# ------------------------------------------------------------ 2. MP report
with open(os.path.join(IN, MP), 'rb') as handle:
    mp_data = handle.read()
rows = read_report(mp_data)
stamps = [d for d in (parse_mp_datetime(r.get('TRANSACTION_DATE')) for r in rows) if d]
incomes_before = set(Collection.search([('kind', '=', 'income')]).mapped('source_id'))
if Report.search_count([('name', '=', MP)]):
    report = Report.search([('name', '=', MP)], limit=1)
    out('SKIP', paso='mp', motivo='reporte ya importado', id=report.id)
else:
    report = Report._import_file(MP, mp_data)
new_incomes = Collection.search([('kind', '=', 'income'), ('source_id', 'not in', list(incomes_before))])
out('RESULT', paso='mp', filas=len(rows), desde=local_when(min(stamps)) if stamps else None,
    hasta=local_when(max(stamps)) if stamps else None, ingresos_nuevos=len(new_incomes),
    total_nuevos=round(sum(new_incomes.mapped('amount')), 2), resumen=report.summary)

# ------------------------------------------------------------ 3. chat
chat = Chat._distri_import(ZIP, DESDE, HASTA, path=os.path.join(IN, ZIP), commit_every=0)
out('RESULT', paso='chat', mensajes=chat.message_count, archivos_nuevos=chat.file_count,
    ya_importados=chat.duplicate_count, otros=chat.skipped_count, faltan=chat.missing_count,
    resumen=chat.summary)

# ------------------------------------------------------------ 3b. same payment, two photos
# The module links the first photo that arrives; when a later photo of the
# same payment brings the operation number and the card holds one without it
# (linked by amount), the card keeps the photo with the number and the other
# is closed as a copy. Only on a pending card, same amount, and only when the
# held photo reads no number or another one.
swaps = []
waiting = Message.search([('distri_chat_import_id', '=', chat.id), ('distri_is_receipt', '=', True),
                          ('distri_receipt_state', 'in', ('pending', 'no_money')),
                          ('distri_operation', '!=', False)], order='timestamp, id')
for msg in waiting:
    number = digits(msg.distri_operation)
    if len(number) < 6:
        continue
    card = Collection.search([('kind', '=', 'income'), '|', ('source_id', '=', number),
                              ('bank_transfer_id', '=', number)])
    held = card.receipt_id
    if len(card) != 1 or card.state != 'draft' or not held or held == msg:
        continue
    if digits(held.distri_operation) and digits(held.distri_operation) in (
            {digits(card.source_id), digits(card.bank_transfer_id)} - {''}):
        continue   # the held photo already proves the operation
    if msg.distri_amount and abs(msg.distri_amount - card.amount) > AMOUNT_TOLERANCE:
        continue
    if held.distri_amount and abs(held.distri_amount - card.amount) > AMOUNT_TOLERANCE:
        continue
    card.receipt_id = False
    msg.with_context(distri_receipt_matching=True).distri_is_receipt = True
    if held.distri_partner_id and not msg.distri_partner_id:
        # the customer the copy named (a seller's note) stays with the payment
        msg.with_context(distri_receipt_matching=True).write({
            'distri_partner_id': held.distri_partner_id.id,
            'distri_partner_source': ('%s (en la copia %s)' % (
                held.distri_partner_source or 'Cliente de la copia', held.media_filename))[:250]})
    card._link_receipt(msg)
    held._distri_close('duplicate', 'Otra foto del mismo pago (operación %s): la tarjeta se queda con %s, '
                       'que trae el número de operación' % (card.source_id, msg.media_filename))
    Collection.search([('kind', 'in', ('receipt', 'verify')), ('receipt_id', '=', held.id),
                       ('state', '=', 'draft')]).write({
        'state': 'ignored', 'ignore_reason': 'receipt_error',
        'ignore_note': 'Copia de %s (operación %s)' % (msg.media_filename, card.source_id)})
    card._propose()
    card._migration_recompute()
    swaps.append(card.id)
    out('LOG', accion='foto_con_numero_reemplaza_copia', tarjeta=card.name, op=card.source_id,
        queda=msg.media_filename, copia=held.media_filename)
out('RESULT', paso='misma_transferencia_dos_fotos', reemplazadas=len(swaps))

# ------------------------------------------------------------ 3c. the manager's cash notices
# The manager turns in no sheet: his notices in the group («Recibí efectivo
# $ …») are the only source of that cash. Channel (scripts/cruce-2026-10-24/
# cargar_efectivo_body.py, step 4 of 24/10): a lab payment in «Efectivo»
# (CSH1), oldest invoice first, key distri_cobro_efwa_<AAAAMMDDhhmm>_<Online
# id>_<amount> in payment_reference; the administrator's Online Excel is only
# for the drivers' paper sheets. Every notice since CASH_SINCE in this export
# and the earlier ones (/in/chats/*.txt) is checked: already in the lab (that
# key, or a cash payment of the customer, same amount, the same day ±1), in
# Online (a posted payment of the customer, same amount, 3 days before to 10
# after), the customer unclear or a wholesaler (WARN, never loaded); else it
# is loaded.
from odoo.addons.distri_pedido.models.cuenta import clave_documento
from odoo.addons.distri_pedido.models.puesta_al_dia import ONLINE_POSTED

CASH_SINCE = date.fromisoformat(os.environ.get('EFECTIVO_DESDE') or '2026-09-15')
CASH_RE = re.compile(r'efectiv', re.I)
AMOUNT_RE = re.compile(r'\$\s*([\d.]+(?:,\d{1,2})?)')
MANAGER_RE = re.compile(r'^distri(\s+neuquen)*$')
NOISE = {'recibi', 'efectivo', 'resto', 'con', 'del', 'los', 'las', 'mensaje', 'edito', 'este'}
Mov = env['distri.cuenta.movimiento'].sudo()
Route = env['distri.route'].sudo()
cash_journal = env['account.journal'].sudo().search([('code', '=', 'CSH1'), ('type', '=', 'cash'),
                                                     ('company_id', '=', env.company.id)])
if len(cash_journal) != 1:
    abort('no hay un único diario Efectivo (CSH1)')


def plain(text):
    text = unicodedata.normalize('NFKD', (text or '').lower())
    return ''.join(ch for ch in text if not unicodedata.combining(ch))


def words(text):
    return {w for w in re.split(r'[^a-z0-9]+', plain(text)) if len(w) >= 3 and not w.isdigit()} - NOISE


notices = {}
chat_texts = [_Source(path=os.path.join(IN, ZIP)).chat_text()]
chats_dir = os.path.join(IN, 'chats')
for name in sorted(os.listdir(chats_dir)) if os.path.isdir(chats_dir) else []:
    with open(os.path.join(chats_dir, name), 'rb') as handle:
        chat_texts.append(handle.read().decode('utf-8-sig', 'replace'))
for text in chat_texts:
    for when, sender, body, att in parse_chat(text):
        if when.date() < CASH_SINCE or not MANAGER_RE.match(plain(sender).strip()):
            continue
        clean = re.sub(r'<[^>]*>', ' ', body or '')
        found = AMOUNT_RE.search(clean)
        if not CASH_RE.search(clean) or not found:
            continue
        amount = float(found.group(1).replace('.', '').replace(',', '.'))
        notices.setdefault((when, amount), (sender, AMOUNT_RE.sub(' ', clean), bool(att)))

customers = Partner.search([('ref', '=like', 'ODOO-%'), ('active', '=', True)]).filtered(
    lambda p: p == p.commercial_partner_id and not p.distri_generic_partner)
customer_words = {p.id: words(p.name) for p in customers}
cash_rows = []
cash_new = Payment.browse()
cash_by_partner = defaultdict(float)
for (when, amount), (sender, text, with_photo) in sorted(notices.items()):
    want = words(text)
    matches = customers.filtered(lambda p: want and want <= customer_words[p.id])
    row = {'fecha': when.strftime('%Y-%m-%d'), 'hora': when.strftime('%H:%M'), 'remitente': sender,
           'importe': amount, 'texto': ' '.join(text.split())[:120], 'con_foto': with_photo,
           'clientes': matches.mapped('name')[:3]}
    partner = matches if len(matches) == 1 else Partner.browse()
    if not partner:
        row['estado'] = 'sin_cliente_unico'
    elif partner.distri_es_mayorista:
        row['estado'] = 'mayorista'
    else:
        day = when.date()
        key = 'distri_cobro_efwa_%s_%s_%s' % (when.strftime('%Y%m%d%H%M'), partner.ref[5:], int(round(amount)))
        lab = Payment.search([('payment_reference', '=', key)]) or Payment.search([
            ('partner_id', 'child_of', partner.id), ('journal_id.type', '=', 'cash'),
            ('payment_type', '=', 'inbound'), ('state', '!=', 'canceled'),
            ('amount', '>=', amount - AMOUNT_TOLERANCE), ('amount', '<=', amount + AMOUNT_TOLERANCE),
            ('date', '>=', day - timedelta(days=1)), ('date', '<=', day + timedelta(days=1))])
        online = env['distri.online.payment'].sudo().search([
            ('online_partner_id', '=', int(partner.ref[5:])), ('state', 'in', ONLINE_POSTED),
            ('amount', '>=', amount - AMOUNT_TOLERANCE), ('amount', '<=', amount + AMOUNT_TOLERANCE),
            ('date', '>=', day - timedelta(days=3)), ('date', '<=', day + timedelta(days=10))])
        row.update(cliente=partner.name, clave=key, en_lab=lab.mapped('name'),
                   en_online=['%s %s (%s)' % (p.name or p.move_name, p.date, p.journal_name) for p in online])
        if lab:
            row['estado'] = 'ya_en_lab'
        elif online:
            row['estado'] = 'ya_en_online'
        else:
            payment = Payment.create({
                'payment_type': 'inbound', 'partner_type': 'customer', 'partner_id': partner.id,
                'amount': amount, 'journal_id': cash_journal.id, 'date': day,
                'memo': 'Efectivo · recibió gerente · aviso WhatsApp %s · avisó %s' % (
                    when.strftime('%d/%m %H:%M'), sender),
                'payment_reference': key, 'company_id': env.company.id})
            payment.action_post()
            Route._apply_oldest_first(payment, partner)
            receivable = payment.move_id.line_ids.filtered(
                lambda l: l.account_id.account_type == 'asset_receivable')
            doc = clave_documento(payment.move_id.name, sum(receivable.mapped('debit')),
                                  sum(receivable.mapped('credit')))
            if payment.move_id.state != 'posted' or Mov.search([('partner_id', '=', partner.id)]).filtered(
                    lambda m: clave_documento(m.comprobante, m.debe, m.haber) == doc):
                abort('el cobro en efectivo %s no quedó bien asentado' % key)
            cash_new |= payment
            cash_by_partner[partner.id] += amount
            row.update(estado='cargar', pago=payment.name)
    cash_rows.append(row)
    out('WARN' if row['estado'] in ('sin_cliente_unico', 'mayorista') else 'CASH', **row)
out('RESULT', paso='efectivo_del_gerente', avisos=len(cash_rows),
    por_estado=dict(Counter(r['estado'] for r in cash_rows)),
    a_cargar=len(cash_new), importe_a_cargar=round(sum(cash_new.mapped('amount')), 2))

# ------------------------------------------------------------ 4. report
source = _Source(path=os.path.join(IN, ZIP))
messages = [m for m in parse_chat(source.chat_text()) if DESDE <= m[0].date() <= HASTA]
texts = [m for m in messages if not m[3] and m[2]]


def notes_near(when, sender):
    """Separate text lines of the same sender within 2 minutes (seller notes)."""
    return ' / '.join(body for t, s, body, _a in texts
                      if s == sender and abs((t - when).total_seconds()) <= 120)[:300]


def card_of(msg):
    card = Collection.search([('kind', '=', 'income'), ('receipt_id', '=', msg.id)], limit=1)
    if not card and msg.distri_collection_id:
        card = msg.distri_collection_id
    if not card:
        card = Collection.search([('receipt_id', '=', msg.id)], order='id desc', limit=1)
    return card


touched = Collection.browse()
files = []
for when, sender, body, att in messages:
    if not att:
        continue
    kind = MEDIA_TYPES.get(os.path.splitext(att)[1].lower())
    raw = source.read(att) if kind and not att.upper().startswith('STK-') else None
    files.append((when, sender, body, att, raw))
for _when, _sender, _body, _att, raw in files:
    if raw is not None:
        msg = Message.search([('wa_message_id', '=', 'chat:' + hashlib.sha256(raw).hexdigest()[:48])], limit=1)
        touched |= card_of(msg) if msg else Collection.browse()
touched._migration_recompute()

rows_out = []
seen_keys = set()
for when, sender, body, att, raw in sorted(files, key=lambda f: f[0], reverse=True):
    row = {'fecha': when.strftime('%Y-%m-%d'), 'hora': when.strftime('%H:%M'), 'remitente': sender,
           'archivo': att, 'texto': body.replace('\n', ' / ')[:300], 'notas': notes_near(when, sender)}
    if raw is None:
        row['categoria'] = 'no_es_archivo_de_comprobante'
        rows_out.append(row)
        continue
    key = 'chat:' + hashlib.sha256(raw).hexdigest()[:48]
    row['clave'] = key
    msg = Message.search([('wa_message_id', '=', key)], limit=1)
    card = card_of(msg) if msg else Collection.browse()
    row.update({
        'importe_leido': msg.distri_amount or '', 'operacion_leida': msg.distri_operation or '',
        'pagador_leido': msg.distri_payer_name or '', 'cuit_leido': msg.distri_payer_cuit or '',
        'banco': msg.distri_bank or '', 'fecha_leida': local_when(msg.distri_receipt_date),
        'es_comprobante': bool(msg.distri_is_receipt), 'estado_comprobante': msg.distri_receipt_state or '',
        'aclaracion': msg.distri_receipt_note or '',
        'tarjeta': card.name or '', 'tarjeta_tipo': card.kind or '', 'tarjeta_estado': card.state or '',
        'mp_operacion': card.source_id or '' if card.kind == 'income' else '',
        'mp_importe': card.amount if card.kind == 'income' else '',
        'mp_fecha': local_when(card.date) if card.kind == 'income' else '',
        'mp_pagador': card.payer_name or '' if card.kind == 'income' else '',
        'cliente': card.partner_id.display_name or msg.distri_partner_id.display_name or '',
        'cliente_fuente': msg.distri_partner_source or '',
        'es_mayorista': bool((card.partner_id or msg.distri_partner_id).commercial_partner_id.distri_es_mayorista),
        'grado': card.money_grade or '', 'caja': card.migration_box or '',
        'fuentes': card.customer_sources or '', 'acuerdo': card.customer_agreement or '',
        'online': card.online_grade or '', 'duplicado': card.migration_duplicate or '',
        'evidencia': card.evidence_note or '',
    })
    row['monto'] = card.amount if card.kind == 'income' else (msg.distri_amount or 0.0)
    if key in seen_keys:
        cat = 'duplicado_en_el_export'
    elif key in keys_before:
        cat = 'ya_en_lab'
    else:
        # the same WhatsApp message of the manager's export with other bytes:
        # same minute and amount, already in the lab
        twin = Message.browse()
        if last_chat and msg.timestamp and msg.timestamp <= last_chat + timedelta(minutes=1):
            twin = Message.search([('id', '!=', msg.id), ('wa_message_id', 'in', list(keys_before)),
                                   ('message_type', '!=', 'text'),
                                   ('timestamp', '>=', msg.timestamp - timedelta(minutes=1)),
                                   ('timestamp', '<=', msg.timestamp + timedelta(minutes=1))]).filtered(
                lambda m: not msg.distri_amount or abs((m.distri_amount or 0) - msg.distri_amount) < 1)
        state = msg.distri_receipt_state
        if twin:
            cat = 'ya_en_lab_mismo_mensaje'
            row['aclaracion'] = ('igual a %s' % twin[0].media_filename + ' · ' + row['aclaracion']).strip(' ·')
        elif not msg.distri_is_receipt or state == 'dismissed':
            cat = 'no_es_comprobante'
        elif state == 'outgoing':
            cat = 'devolucion_o_pago_propio'
        elif card.migration_box == 'online' or (state == 'registered' and 'Online' in (msg.distri_receipt_note or '')):
            cat = 'ya_en_online'
        elif state == 'registered':
            cat = 'ya_confirmado_en_lab'
        elif state == 'duplicate' or card.migration_box == 'duplicate':
            cat = 'duplicado'
        elif row['es_mayorista'] or (card.state == 'apartado'):
            cat = 'mayorista'
        else:
            cat = 'nuevo'
    seen_keys.add(key)
    row['categoria'] = cat
    rows_out.append(row)

for row in rows_out:
    out('ROW', **row)

by_cat = Counter(r['categoria'] for r in rows_out)
new_rows = [r for r in rows_out if r['categoria'] == 'nuevo']
boxes = defaultdict(lambda: [0, 0.0])
for r in new_rows:
    k = '%s/%s' % (r['caja'] or r['estado_comprobante'] or '-', r['grado'] or '-')
    boxes[k][0] += 1
    boxes[k][1] += float(r['monto'] or 0)
odd = {
    'sin_movimiento_mp': sum(1 for r in new_rows if r['caja'] == 'waiting_money' or r['estado_comprobante'] in ('pending', 'no_money')),
    'sin_cliente': sum(1 for r in new_rows if not r['cliente']),
    'devoluciones_mp_en_tarjetas': Collection.search_count([('id', 'in', touched.ids), ('reversal', '!=', False)]),
}
mp_with_photo = new_incomes.filtered('receipt_id')
out('RESULT', paso='resumen', archivos=len(files), por_categoria=dict(by_cat),
    nuevos_por_caja_grado={k: [v[0], round(v[1], 2)] for k, v in sorted(boxes.items())},
    nuevos_total=round(sum(float(r['monto'] or 0) for r in new_rows), 2), raros=odd,
    mp_nuevos_con_foto=len(mp_with_photo), mp_nuevos_sin_foto=len(new_incomes) - len(mp_with_photo),
    mp_nuevos_sin_foto_total=round(sum((new_incomes - mp_with_photo).mapped('amount')), 2),
    mp_nuevos_apartados=len(new_incomes.filtered(lambda c: c.state == 'apartado')),
    mp_nuevos_en_online=len(new_incomes.filtered(lambda c: c.migration_box == 'online')),
    tarjetas_del_tablero=dict(Counter(Collection.search([('receipt_id', '!=', False)]).mapped('migration_box'))))

# ------------------------------------------------------------ guards
env.flush_all()
if Payment.search_count([]) != payments_before + len(cash_new):
    abort('se crearon pagos fuera del efectivo del gerente')
for partner in all_partners:
    moved = balances_before[partner.id] - partner._distri_account_balance()
    if abs(moved - cash_by_partner.get(partner.id, 0.0)) > 0.01:
        abort('el saldo del cliente %s bajó %s y no %s' % (partner.ref, moved, cash_by_partner.get(partner.id, 0.0)))
changed = [(c.name, cards_before[c.id], c.state) for c in Collection.browse(list(cards_before)).exists()
           if c.state != cards_before[c.id]]
# a «Sin transferencia» card closing because its money arrived is the
# module's normal path; anything else on an old card stops the load
bad = [c for c in changed if not (c[1] == 'draft' and c[2] == 'ignored')]
out('RESULT', paso='tarjetas_viejas', cambiaron=len(changed), cerradas_por_llegar_la_plata=len(changed) - len(bad))
if bad:
    abort('tarjetas existentes cambiaron de estado: %s' % bad[:5])
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
