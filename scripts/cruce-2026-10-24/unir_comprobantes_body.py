# Step 14 of the 24/10/2026 cross (odoo shell body, run by paso14-unir-comprobantes.sh).
#
# After the manager's WhatsApp chat is imported into the module
# (scripts/importar-chat-whatsapp.sh), every receipt is tied to the transfer
# the cross decided (paso14_comprobantes.csv, built by
# insumos/cruce-2026-10-24/scripts/armar_paso14.py from cruce_final.csv plus
# the vision review of the 553 weak or unlinked receipts and its blind check),
# not to the one the module's own OCR picked:
# - vinculo: the receipt goes on the income card of that operation
#   (_link_receipt: the card shows the photo, the receipt's «Sin
#   transferencia» and «Esperando reporte» cards close). Every link the module
#   made to another transfer is undone first, so the order of the file does
#   not matter; the cards touched get their proposed customer again.
# - duplicado / no_comprobante / sin_transferencia / devolucion: the receipt
#   is closed («Ya recibido» / «Descartado» / «Pago saliente») with the
#   reason, and so is its «Sin transferencia» card.
# The module stores one message per file hash (wa_message_id «chat:<sha256>»,
# the CSV's «clave»): a photo WhatsApp sent twice byte for byte (Santander's
# narrow receipts, «same amount, same accounts») is one message for several
# payments. It goes to the payment still pending (where it helps to confirm),
# else to the first one; the rest are logged. A second, different photo of a
# payment that already has one is closed as a copy.
# Guards (ABORT, nothing written): a payment created, a customer balance
# moved, an income card changing state, a payment promise changing, a photo
# taken off a card that is not pending, a receipt of the file not on the card
# the cross gives it, a pending card proposed by a photo it no longer has.
# Idempotent. Rolls back unless DO_COMMIT=1.
import csv
import json
import os
from collections import Counter, defaultdict

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IN = os.environ.get('IN_DIR', '/cruce')
CROSS = '(cruce del 25/10/2026)'
CLOSED = ('dismissed', 'duplicate', 'outgoing', 'registered')
# reason of the receipt's own «Sin transferencia» card once the receipt is
# closed (the module's mapping in _distri_sync_verify_card, which only closes
# the «Esperando reporte» card)
CARD_REASON = {'dismissed': 'not_receipt', 'duplicate': 'receipt_error', 'registered': 'receipt_late',
               'outgoing': 'own'}

Partner = env['res.partner'].sudo().with_context(active_test=False)
Payment = env['account.payment'].sudo()
Collection = env['distri.mp.collection'].sudo()
Message = env['distri.whatsapp.message'].sudo()


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


def log(**data):
    print('LOG', json.dumps(dict(paso=14, **data), ensure_ascii=False, default=str))


with open(os.path.join(IN, 'paso14_comprobantes.csv'), encoding='utf-8') as handle:
    rows = list(csv.DictReader(handle))
by_key = defaultdict(list)
for row in rows:
    by_key[row['clave']].append(row)

payments_before = Payment.search_count([])
all_partners = Partner.search([('ref', '=like', 'ODOO-%'), ('active', '=', True)]).filtered(
    lambda p: p == p.commercial_partner_id)
balances_before = {p.id: p._distri_account_balance() for p in all_partners}
board = {c.source_id: c for c in Collection.search([('kind', '=', 'income')])}
states_before = {c.id: c.state for c in board.values()}
Promise = env['distri.promised.transfer'].sudo().with_context(active_test=False)
promises_before = sorted(Promise.search([]).mapped(lambda p: (p.id, p.state, p.receipt_id.id)))

# matched by the importer's key (sha256 of the original file): the stored
# attachment's checksum is not the file's, Odoo shrinks large photos on save
messages = Message.search([('wa_message_id', 'in', list(by_key)), ('attachment_id', '!=', False)])
by_message_key = {msg.wa_message_id: msg for msg in messages}
stats = Counter()


def linked_rows(items):
    """The mentions that tie the file to a transfer on the board, the one
    that gets the photo first: a pending card, then the first mention."""
    linked = [r for r in items if r['estado'] == 'vinculo' and r['mp_op']]
    return sorted(linked, key=lambda r: (board[r['mp_op']].state != 'draft' if r['mp_op'] in board else True,
                                         r['t']))


plan = {key: linked_rows(items) for key, items in by_key.items()}
targets = {key: board.get(linked[0]['mp_op']) if linked else None for key, linked in plan.items()}
touched = Collection.browse()

# 1. undo every link the module made to a transfer the cross does not give
# that receipt, before linking: otherwise the order of the file decides
for key, msg in by_message_key.items():
    for other in Collection.search([('kind', '=', 'income'), ('receipt_id', '=', msg.id)]):
        if other != targets.get(key):
            if other.state != 'draft':
                abort('el comprobante %s está en la tarjeta %s, que no está pendiente (%s)'
                      % (msg.media_filename, other.source_id, other.state))
            other.receipt_id = False
            touched |= other
            stats['corregido_del_modulo'] += 1
            log(accion='corregido_del_modulo', archivo=msg.media_filename,
                op=targets[key].source_id if targets.get(key) else None, modulo=other.source_id)


def link(card, msg):
    # _link_receipt does not set it: «Comprobantes» and copies need it. The
    # context keeps the write from resolving payment promises on its own
    msg.with_context(distri_receipt_matching=True).distri_is_receipt = True
    card._link_receipt(msg)


# 2. link or close
for key, items in by_key.items():
    msg = by_message_key.get(key)
    if not msg:
        stats['sin_mensaje_importado:%s' % items[0]['estado']] += 1
        continue
    linked = plan[key]
    if linked:
        first = linked[0]
        card = targets[key]
        if not card:
            stats['op_sin_tarjeta'] += 1
            log(accion='op_sin_tarjeta', archivo=first['archivo'], op=first['mp_op'])
            continue
        if card.receipt_id == msg:
            if msg.distri_receipt_state != 'matched' or msg.distri_collection_id != card or not msg.distri_is_receipt:
                link(card, msg)   # on its card but closed or pointing elsewhere: repair
                stats['reparado'] += 1
            else:
                stats['ya_unido'] += 1
        elif card.receipt_id and targets.get(card.receipt_id.wa_message_id) == card:
            # the cross gives this transfer two photos: the same receipt sent
            # again (photo and PDF, or forwarded later); the second is a copy
            if msg.distri_receipt_state != 'duplicate':
                msg._distri_close('duplicate', 'Otra foto del mismo pago (operación %s) %s'
                                  % (card.source_id, CROSS))
                log(accion='otra_foto_mismo_pago', archivo=first['archivo'], op=card.source_id,
                    tiene=card.receipt_id.media_filename)
            stats['otra_foto_mismo_pago'] += 1
        elif card.receipt_id:
            stats['tarjeta_con_otro_comprobante'] += 1
            log(accion='tarjeta_con_otro_comprobante', archivo=first['archivo'], op=card.source_id,
                tiene=card.receipt_id.media_filename)
        else:
            link(card, msg)
            touched |= card
            stats['unido:%s' % first['fuente'].split(':')[0]] += 1
        for row in linked[1:]:
            if row['mp_op'] != first['mp_op']:
                stats['foto_repetida_otro_pago'] += 1
                log(accion='foto_repetida_otro_pago', archivo=row['archivo'], t=row['t'], op=row['mp_op'],
                    foto_en=first['mp_op'])
        continue
    state = items[0]['estado']
    if msg.distri_receipt_state in CLOSED:
        stats['ya_cerrado'] += 1
        continue
    if state == 'duplicado':
        msg._distri_close('duplicate', 'Copia de otro comprobante %s' % CROSS)
    elif state == 'devolucion':
        msg._distri_close('outgoing', 'Devolución a un cliente: transferencia saliente de Mercado Pago%s %s'
                          % (' (operación %s)' % items[0]['mp_op'] if items[0]['mp_op'] else '', CROSS))
    elif state in ('no_comprobante', 'sin_transferencia'):
        msg._distri_close('dismissed', 'Sin transferencia de cliente en Mercado Pago: pago a terceros, '
                                       'efectivo o no es comprobante %s' % CROSS)
    else:
        stats['sin_decision:%s' % state] += 1
        continue
    stats['cerrado:%s' % state] += 1

# 3. the «Sin transferencia» card of every closed receipt
for msg in by_message_key.values():
    reason = CARD_REASON.get(msg.distri_receipt_state)
    if not reason:
        continue
    cards = Collection.search([('kind', '=', 'receipt'), ('receipt_id', '=', msg.id), ('state', '=', 'draft')])
    if cards:
        cards.write({'state': 'ignored', 'ignore_reason': reason,
                     'ignore_note': (msg.distri_receipt_note or CROSS)[:250]})
        stats['tarjeta_sin_transferencia_cerrada'] += len(cards)

# 4. the proposed customer of every pending card whose photo changed
pending = touched.filtered(lambda c: c.state == 'draft')
pending._propose()
stats['propuesta_recalculada'] += len(pending)

# ------------------------------------------------------------------ guards
env.flush_all()
if Payment.search_count([]) != payments_before:
    abort('se crearon pagos')
for partner in all_partners:
    if abs(partner._distri_account_balance() - balances_before[partner.id]) > 0.01:
        abort('cambió el saldo del cliente %s' % partner.ref)
for card in board.values():
    if card.state != states_before[card.id]:
        abort('la tarjeta %s cambió de estado (%s -> %s)' % (card.source_id, states_before[card.id], card.state))
for key, msg in by_message_key.items():
    card = targets.get(key)
    if card and card.receipt_id != msg and not (
            card.receipt_id and targets.get(card.receipt_id.wa_message_id) == card):
        abort('el comprobante %s no quedó en la operación %s' % (msg.media_filename, card.source_id))
    holders = Collection.search([('kind', '=', 'income'), ('receipt_id', '=', msg.id)])
    if holders - (card or Collection.browse()):
        abort('el comprobante %s quedó en otra tarjeta (%s)' % (msg.media_filename, holders.mapped('source_id')))
if sorted(Promise.search([]).mapped(lambda p: (p.id, p.state, p.receipt_id.id))) != promises_before:
    abort('cambiaron las promesas de pago')
stale = Collection.search([('kind', '=', 'income'), ('state', '=', 'draft'), ('receipt_id', '=', False),
                           ('proposal', 'in', ('photo', 'operation'))])
if stale & touched:
    abort('%s tarjetas pendientes proponen un cliente por una foto que ya no tienen: %s'
          % (len(stale & touched), (stale & touched)[:5].mapped('source_id')))
receipts = Message.search([('distri_is_receipt', '=', True)])
print('RESULT', json.dumps({**dict(stats),
                            'propuesta_foto_sin_foto_fuera_del_paso': len(stale - touched),
                            'comprobantes': dict(Counter(receipts.mapped(lambda m: m.distri_receipt_state or '-'))),
                            'tarjetas_con_foto': Collection.search_count([('kind', '=', 'income'), ('receipt_id', '!=', False)]),
                            'tablero': dict(Counter(Collection.search([]).mapped(lambda c: '%s:%s' % (c.kind, c.state))))},
                           ensure_ascii=False))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
