# Cash sheets into the lab, step 2 (odoo shell body, run by cargar-planillas.sh
# after preparar.py): /in/entrada.json + /in/files/. Writes only
# distri.cash.sheet, distri.cash.sheet.line and their ir.attachment; never
# creates, changes, posts or cancels a payment or a move (a line only points
# at a payment). Idempotent: a sheet already loaded (same source_key) is not
# loaded again, a line already linked is left as it is, an original already
# attached is not attached again. Rolls back unless DO_COMMIT=1.
#
# Order:
#  1. the sheets of the 2026 archive (one per tab of an xlsx), with the xlsx;
#  2. the loader rows loaded from a row of the archive (fila = mov id), then
#     the pairings _cruce-online/pareos.csv: only odoo_source=lab, the payment
#     exists, is inbound and not cancelled, and is on no other row;
#  3. customer of each row: the paired payment's customer, else the Online
#     customer of the cross (pareo or mapeo-clientes alta/media, rows from
#     March 2026 on) through ref ODOO-<id>;
#  4. the cash loader rows (JSONL), linked by clave = payment_reference
#     (models/cash_sheet.py _link_loader_row), with the Drive download of the
#     sheet when there is one;
#  5. CSH1 payments with a loader memo still on no row
#     (_link_by_memo: collector + day + invoice, unique);
#  6. photos: attached to the one sheet whose rows carry their invoices.
import collections
import json
import os

from odoo.addons.distri_pedido.models.cash_sheet import MOV_RE, name_tokens

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IN = os.environ.get('IN_DIR', '/in')

Sheet = env['distri.cash.sheet'].sudo()
Line = env['distri.cash.sheet.line'].sudo()
Payment = env['account.payment'].sudo()
Partner = env['res.partner'].sudo().with_context(active_test=False)
MIMES = {'.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
         '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.txt': 'text/plain'}


def out(tag, **data):
    print(tag, json.dumps(data, ensure_ascii=False, default=str))


def raw(rel):
    with open(os.path.join(IN, rel), 'rb') as handle:
        return handle.read()


def mime(name):
    return MIMES.get(os.path.splitext(name)[1].lower(), 'application/octet-stream')


with open(os.path.join(IN, 'entrada.json'), encoding='utf-8') as handle:
    bundle = json.load(handle)
stats = collections.Counter()
before = {'sheets': Sheet.search_count([]), 'lines': Line.search_count([]),
          'linked': Line.search_count([('payment_id', '!=', False)])}
payments_before = Payment.search_count([])
payment_amounts_before = sum(Payment.search([]).mapped('amount'))

# ------------------------------------------------------------------ 1. xlsx
existing = set(Sheet.search([]).mapped('source_key'))
for item in bundle['sheets']:
    if item['key'] in existing:
        stats['planillas_ya_cargadas'] += 1
        continue
    sheet = Sheet.create({
        'name': item['name'], 'collector': item['collector'], 'date': item['date'],
        'origin': 'xlsx', 'source_path': item['path'], 'source_tab': item['tab'],
        'sha256': item['sha256'], 'source_key': item['key'], 'note': item['note'] or False,
        'line_ids': [(0, 0, {
            'sequence': l['sequence'], 'source_cell': l['cell'], 'source_key': l['key'],
            'client_raw': l['client'] or False, 'invoice_ref': l['invoice'] or False,
            'amount_invoiced': l['invoiced'], 'amount_paid': l['paid'], 'method': l['method'] or False,
            'notes': l['notes'] or False, 'row_kind': l['kind'], 'after_total': l['after_total'],
            'dup_status': l['dup'] or False, 'row_date': l['date'],
        }) for l in item['lines']],
    })
    sheet._attach(item['file_name'], raw(item['file']), mime(item['file_name']))
    stats['planillas_xlsx'] += 1
    stats['filas_xlsx'] += len(item['lines'])
env.flush_all()
for rel, names in bundle['skipped'].items():
    out('OMITIDO', motivo=rel, archivos=len(names), ejemplos=names[:3])

# ------------------------------------------------------------------ loader rows
# A loader row whose fila is a row of the archive (mov id) was loaded from that
# very row: it is linked before the pairings, which are a heuristic.
loader_stats = collections.Counter()


def link_loader(item):
    pay = Payment.search([('payment_reference', '=', item['clave'])], limit=1)
    if pay and (pay.payment_type != 'inbound' or pay.state == 'canceled'):
        loader_stats['pago cancelado o no de entrada: fila sin pago'] += 1
        pay = Payment
    had = bool(pay) and bool(Line.search_count([('payment_id', '=', pay.id)]))
    line = Sheet._link_loader_row(item, pay or None)
    if had:
        loader_stats['ya en una fila'] += 1
    elif not line:
        loader_stats['sin pago, el día está en una planilla Excel: no se agrega'] += 1
    elif pay and line.payment_id == pay:
        loader_stats['vinculada (%s)' % line.link_kind] += 1
    elif pay:
        loader_stats['fila ya vinculada a otro pago'] += 1
        out('CARGA_CONFLICTO', fila=item['fila'], clave=item['clave'], linea=line.id,
            pago_fila=line.payment_id.name, pago=pay.name)
    else:
        loader_stats['fila sin pago en el lab'] += 1


archive_rows = [i for i in bundle['loader'] if MOV_RE.match(i.get('fila') or '')]
for item in archive_rows:
    link_loader(item)
env.flush_all()

# ------------------------------------------------------------------ 2. pareos
by_key = {l.source_key: l for l in Line.search([('source_key', '!=', False)])}
taken = set(Line.search([('payment_id', '!=', False)]).mapped('payment_id').ids)
pair_stats = collections.Counter()
for pair in bundle['pairs']:
    kind = pair['match_kind']
    if pair['odoo_source'] != 'lab':
        pair_stats[(kind, 'online (no se vincula)')] += 1
        continue
    line = by_key.get(pair['mov_id'])
    pay = Payment.browse(int(pair['odoo_id'][3:])).exists() if pair['odoo_id'].startswith('LAB') else Payment
    if not line:
        reason = 'fila no cargada (copia u omitida)'
    elif not pay:
        reason = 'el pago no existe'
    elif pay.payment_type != 'inbound':
        reason = 'el pago no es de entrada'
    elif pay.state == 'canceled':
        reason = 'el pago está cancelado'
    elif line.payment_id == pay:
        reason = 'ya vinculado'
    elif line.payment_id or pay.id in taken:
        reason = 'fila o pago ya vinculados a otro'
    else:
        reason = ''
    if reason:
        pair_stats[(kind, reason)] += 1
        if reason not in ('ya vinculado',):
            out('PAREO_OMITIDO', mov_id=pair['mov_id'], pago=pair['odoo_name'], tipo=kind, motivo=reason)
        continue
    line.write({'payment_id': pay.id, 'link_kind': 'pareo', 'match_kind': kind,
                'partner_id': pay.partner_id.commercial_partner_id.id})
    taken.add(pay.id)
    pair_stats[(kind, 'vinculado')] += 1
for (kind, what), count in sorted(pair_stats.items()):
    out('PAREOS', tipo=kind, resultado=what, filas=count)

# ------------------------------------------------------------------ 3. customers
# 3a. by invoice first: a client row whose invoice number is an invoice of
# the account history (distri.cuenta.movimiento, synced from Online) of
# exactly one customer is that customer's row, whatever name the seller
# wrote (owner, 13/11/2026: every row of the archive, old ones included).
# A mistyped or carried-over invoice would win silently, so when the written
# name shares no word with the invoice's customer the row is NOT assigned and
# is listed (FACTURA_OTRO_NOMBRE) for the owner to check: the 13/11 dry run
# showed real wrong customers there (e.g. «LIBRERIA LANIN» on an invoice of
# an Axion station), next to spelling variants of the same person.
# Words of a trade or a town say nothing about who the customer is.
GENERIC_WORDS = {
    'KIOSCO', 'DESPENSA', 'ALMACEN', 'MERCADO', 'MINIMERCADO', 'AUTOSERVICIO', 'VERDULERIA',
    'FIAMBRERIA', 'LIBRERIA', 'SUPER', 'SUPERMERCADO', 'MARKET', 'MINIMARKET', 'MINI', 'MARK',
    'POLLERIA', 'CARNICERIA', 'PANADERIA', 'AGENCIA', 'QUINIELA', 'BARRIO', 'LOTEO', 'SOCIAL',
    'CENTENARIO', 'PLOTTIER', 'ZAPALA', 'NEUQUEN', 'SENILLOSA', 'CIPOLLETTI', 'CUTRAL',
    'PLAZA', 'HUINCUL', 'CHOCON', 'PAGO', 'DESTINO'}
# Customers renamed in Odoo (owner): (words written on the sheets, words of
# the customer's current name). NAVARRETE MARTIN is now ERIC OLAVE (13/11/2026);
# NAVARRETE HUGO (Jamón Jamón) is another customer and keeps no alias.
KNOWN_RENAMES = [({'NAVARRETE', 'MARTIN'}, {'ERIC', 'OLAVE'})]


def renamed(written, current):
    return any(old <= written and new <= current for old, new in KNOWN_RENAMES)


Movement = env['distri.cuenta.movimiento'].sudo().with_context(active_test=False)
by_invoice = Line.search([('partner_id', '=', False), ('row_kind', '=', 'cliente'),
                          ('invoice_ref', '!=', False)])
for line in by_invoice:
    partners = Movement.search([('comprobante', '=', line.invoice_ref)]).mapped(
        'partner_id').commercial_partner_id
    if len(partners) == 1:
        written = name_tokens(line.client_raw or '') - GENERIC_WORDS
        current = name_tokens(partners.name) - GENERIC_WORDS
        if written and not written & current and not renamed(written, current):
            stats['factura_otro_nombre_sin_asignar'] += 1
            out('FACTURA_OTRO_NOMBRE', linea=line.id, planilla=line.sheet_id.name,
                fecha=line.row_date, escrito=line.client_raw, factura=line.invoice_ref,
                cliente=partners.name, pagado=line.amount_paid)
            continue
        line.partner_id = partners
        stats['clientes_por_factura'] += 1
    else:
        stats['factura_sin_cliente_unico'] += 1
        out('FACTURA_SIN_CLIENTE', linea=line.id, factura=line.invoice_ref, clientes=len(partners))
env.flush_all()

# 3b. by the name cross (pareos / mapeo-clientes) for the rows still without one
refs = {}
for sheet in bundle['sheets']:
    for l in sheet['lines']:
        if l['online_partner']:
            refs[l['key']] = l['online_partner']
lab_of_online = {}
for online in set(refs.values()):
    found = Partner.search([('ref', '=', 'ODOO-%s' % online)]).commercial_partner_id
    lab_of_online[online] = found if len(found) == 1 else Partner
for line in Line.search([('partner_id', '=', False), ('source_key', 'in', list(refs))]):
    partner = lab_of_online.get(refs[line.source_key])
    if partner:
        line.partner_id = partner
        stats['clientes_asignados'] += 1
    else:
        stats['clientes_sin_ficha_en_lab'] += 1

# ------------------------------------------------------------------ 4. loader rows
for item in bundle['loader']:
    if item not in archive_rows:
        link_loader(item)
for what, count in sorted(loader_stats.items()):
    out('CARGA', resultado=what, filas=count)
env.flush_all()
for drive in bundle['drive']:
    key = 'carga:%s:%s:%d' % (drive['collector'], drive['date'], drive['index'])
    sheet = Sheet.search([('source_key', '=', key)])
    if not sheet:
        out('DRIVE_SIN_PLANILLA', archivo=drive['name'], clave=key)
        continue
    if sheet._attach(drive['name'], raw(drive['file']), 'text/plain'):
        stats['drive_adjuntos'] += 1

# ------------------------------------------------------------------ 5. memo
cash = Payment.search([('journal_id.code', '=', 'CSH1'), ('payment_type', '=', 'inbound'),
                       ('state', '!=', 'canceled')])
linked_ids = set(Line.search([('payment_id', 'in', cash.ids)]).mapped('payment_id').ids)
memo_stats = collections.Counter()
for pay in cash.filtered(lambda p: p.id not in linked_ids).sorted('date'):
    if not Sheet._memo_parts(pay):
        continue
    line = Sheet._link_by_memo(pay)
    memo_stats['vinculado (%s)' % line.link_kind if line else 'sin fila única'] += 1
    if not line:
        out('MEMO_SIN_FILA', pago=pay.name, fecha=pay.date, importe=pay.amount, memo=pay.memo)
for what, count in sorted(memo_stats.items()):
    out('MEMO', resultado=what, pagos=count)

# ------------------------------------------------------------------ 6. photos
env.flush_all()
photo_stats = collections.Counter()
for photo in bundle['photos']:
    hits = collections.Counter()
    for line in Line.search([('invoice_ref', 'in', photo['invoices']), ('sheet_id.origin', '=', 'xlsx')]):
        hits[line.sheet_id] += 1
    best = hits.most_common(2)
    clear = best and best[0][1] >= min(2, len(photo['invoices'])) and \
        (len(best) == 1 or best[0][1] >= 2 * best[1][1])
    if not clear:
        photo_stats['sin planilla clara'] += 1
        out('FOTO_SIN_PLANILLA', foto=photo['name'], facturas=photo['invoices'],
            candidatas=[(s.name, n) for s, n in best])
        continue
    sheet = best[0][0]
    if sheet._attach(photo['name'], raw(photo['file']), mime(photo['name'])):
        photo_stats['adjuntada'] += 1
        out('FOTO', foto=photo['name'], planilla=sheet.name, fecha=sheet.date, coincidencias=best[0][1],
            de=len(photo['invoices']))
    else:
        photo_stats['ya adjuntada'] += 1
for what, count in sorted(photo_stats.items()):
    out('FOTOS', resultado=what, fotos=count)

# ------------------------------------------------------------------ guards
env.flush_all()
if Payment.search_count([]) != payments_before or \
        abs(sum(Payment.search([]).mapped('amount')) - payment_amounts_before) > 0.005:
    env.cr.rollback()
    print('ABORT cambiaron los pagos del lab')
    raise SystemExit(1)
env.cr.execute("select payment_id from distri_cash_sheet_line where payment_id is not null "
               "group by 1 having count(*) > 1")
dup = env.cr.fetchall()
if dup:
    env.cr.rollback()
    print('ABORT un pago en dos filas', dup)
    raise SystemExit(1)

# ------------------------------------------------------------------ summary
cash = Payment.search([('journal_id.code', '=', 'CSH1'), ('payment_type', '=', 'inbound'),
                       ('state', '!=', 'canceled')])
linked_ids = set(Line.search([('payment_id', 'in', cash.ids)]).mapped('payment_id').ids)
loose = cash.filtered(lambda p: p.id not in linked_ids)
by_month = collections.Counter(p.date.strftime('%Y-%m') for p in loose)


def memo_kind(memo):
    memo = memo or ''
    if memo.startswith('Efectivo · cobró') and ' · planilla ' in memo:
        return 'memo de planilla'
    if memo.startswith('Efectivo · recibió'):
        return 'aviso por WhatsApp'
    if 'depósito' in memo:
        return 'cobrado en el depósito'
    if memo.startswith('Efectivo · auditoría'):
        return 'auditoría'
    return 'otro'


by_kind = collections.Counter(memo_kind(p.memo) for p in loose)
for pay in loose.sorted('date'):
    out('CSH1_SIN_FILA', pago=pay.name, fecha=pay.date, importe=pay.amount, cliente=pay.partner_id.name,
        memo=pay.memo, tipo=memo_kind(pay.memo))
sheets = Sheet.search([])
no_photo = sheets.filtered(lambda s: s.origin != 'xlsx' and not s.has_photo)
for sheet in no_photo.sorted('date'):
    out('PLANILLA_SIN_FOTO', planilla=sheet.name, fecha=sheet.date, filas=sheet.line_count,
        adjuntos=sheet.attachment_ids.mapped('name'))
out('RESULT', **stats,
    planillas=len(sheets), planillas_nuevas=len(sheets) - before['sheets'],
    planillas_por_origen=dict(collections.Counter(sheets.mapped('origin'))),
    planillas_con_foto=len(sheets.filtered('has_photo')),
    filas=Line.search_count([]), filas_nuevas=Line.search_count([]) - before['lines'],
    filas_con_pago=Line.search_count([('payment_id', '!=', False)]),
    filas_con_pago_nuevas=Line.search_count([('payment_id', '!=', False)]) - before['linked'],
    filas_con_cliente=Line.search_count([('partner_id', '!=', False)]),
    csh1_pagos=len(cash), csh1_con_fila=len(linked_ids), csh1_sin_fila=len(loose),
    csh1_sin_fila_por_mes=dict(sorted(by_month.items())), csh1_sin_fila_por_tipo=dict(by_kind),
    transcripciones_sin_foto=len(no_photo))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
