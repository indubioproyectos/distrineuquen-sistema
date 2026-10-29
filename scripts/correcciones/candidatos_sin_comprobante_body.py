# «Candidatos sin comprobante» (control 28/10/2026): the confirmed Mercado Pago
# cards of insumos/control-2026-10-28/pagos-lab-sin-comprobante.csv (empty
# `nota`) were assigned on 24-25/10 by payer CUIT/name, have NO receipt in the
# WhatsApp group and are not in Online. Rule: only a payment with its receipt
# in the group (and matching the MP report) reduces a debt. Each card goes back
# to «Pendiente» with its customer kept (the board's own «Deshacer»: the PMP
# payment is cancelled, its reconciliations undone), so it stays listed per
# customer («Revisión por cliente» → «Importe sin comprobante (no aplicado)»)
# and never reduces the balance until the receipt arrives.
#
# Exceptions (never undone here):
#   27655 MELI - BARDA DEL MEDIO: balance fixed at $0 with management.
#   27931 TOMY - CHINO DE LA ESQUINA: confirmed by a person on 26/10 together
#         with the unrecorded sale VSR/2026/00001 (undoing only the payment
#         would leave $195.000 of debt).
#   27764 27617 27334 27281 27265 27245: the receipt exists (PDF of the
#         administrator's export of the group + the legend message at the same
#         minute): they keep their payment; link the receipt instead.
#   Any card whose payment is reconciled or was confirmed by a person: listed
#         as SKIP, decided one by one.
# Junk: 26703 ($2): undone and discarded (reason «other»).
#
# Needs the guard field `receipt_candidate` on distri.mp.collection (keeps the
# card out of «Confirmar todos» and blocks confirming it without a receipt).
# Without that field it only runs as a dry run.
# Rolls back unless DO_COMMIT=1. Never run with DO_COMMIT=1 before the module
# change is installed. Input: CSV_PATH (default /in/pagos-lab-sin-comprobante.csv).
import csv
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CSV_PATH = os.environ.get('CSV_PATH', '/in/pagos-lab-sin-comprobante.csv')
KEEP = {27655: 'MELI: saldo en $0 acordado con gerencia',
        27931: 'confirmado a mano con la venta sin registrar VSR/2026/00001'}
WITH_RECEIPT = {27764, 27617, 27334, 27281, 27265, 27245}
JUNK = {26703}
NOTE = ('Candidato · falta el comprobante (control 28/10/2026): asignado el 24-25/10 por CUIT o '
        'nombre del pagador, sin comprobante en el grupo de WhatsApp. No baja la deuda hasta que '
        'el cliente mande el comprobante y coincida con el reporte de Mercado Pago.')

C = env['distri.mp.collection'].sudo()
has_flag = 'receipt_candidate' in C._fields
rows = [r for r in csv.DictReader(open(CSV_PATH, encoding='utf-8')) if not r['nota'].strip()]
cards = C.browse([int(r['tarjeta']) for r in rows]).exists()
assert len(cards) == len(rows), 'missing cards: %s' % (len(rows) - len(cards))
bot = env.ref('base.user_root')


def problem(card):
    if card.id in KEEP:
        return 'KEEP ' + KEEP[card.id]
    if card.id in WITH_RECEIPT:
        return 'RECEIPT existe el comprobante: vincularlo, no deshacer'
    if card.state != 'confirmed' or card.kind != 'income':
        return 'STATE %s/%s' % (card.state, card.kind)
    if card.receipt_id:
        return 'RECEIPT ya tiene comprobante %s' % card.receipt_id.id
    if card.online_payment_id or card.export_batch_id:
        return 'ONLINE vinculado o exportado'
    line = card.payment_id.move_id.line_ids.filtered(
        lambda l: l.account_id.account_type == 'asset_receivable')
    if line.matched_debit_ids or line.matched_credit_ids:
        return 'RECONCILED %s' % ', '.join(line.matched_debit_ids.mapped('debit_move_id.move_id.name'))
    if card.confirmed_by_id and card.confirmed_by_id not in (bot, env.ref('base.user_admin')) \
            and card.confirmed_by_id.name != 'OdooBot':
        return 'PERSON confirmado por %s' % card.confirmed_by_id.name
    return ''


todo, skipped = C.browse(), []
for card in cards:
    why = problem(card)
    if why:
        skipped.append((card, why))
    else:
        todo |= card
partners = todo.mapped('partner_id.commercial_partner_id')
before = {p.id: p._distri_account_balance() for p in partners}

todo.action_undo()
junk = todo.filtered(lambda c: c.id in JUNK)
candidates = todo - junk
vals = {'proposal_note': NOTE}
if has_flag:
    vals['receipt_candidate'] = True
candidates.write(vals)
for card in candidates:
    card.message_post(body=NOTE)
junk.write({'state': 'ignored', 'ignore_reason': 'other',
            'ignore_note': 'Transferencia de prueba ($2): no es un pago (control 28/10/2026)'})
env.flush_all()
todo._migration_recompute()

after = {p.id: p._distri_account_balance() for p in partners}
diff_total = sum(after[k] - before[k] for k in before)
print('RESULT has_flag=%s target=%d undone=%d candidates=%d junk=%d skipped=%d amount_undone=%.2f '
      'balance_increase=%.2f' % (has_flag, len(cards), len(todo), len(candidates), len(junk),
                                 len(skipped), sum(todo.mapped('amount')), diff_total))
for card, why in skipped:
    print('SKIP', card.id, card.amount, card.partner_id.name, '|', why)
by_partner = {}
for card in candidates:
    by_partner.setdefault(card.partner_id.commercial_partner_id, []).append(card)
for p, cs in sorted(by_partner.items(), key=lambda kv: -sum(c.amount for c in kv[1])):
    print('CLIENTE %s | candidatos %d por %.2f | saldo %.2f -> %.2f' % (
        p.name, len(cs), sum(c.amount for c in cs), before[p.id], after[p.id]))
# guard check: with the field the cards are out of «Confirmar todos»
try:
    ready, _skip = C._ready_to_confirm()
    print('GUARD in_confirmar_todos=%d' % len(ready & candidates))
except Exception as exc:
    print('GUARD', exc)
ok = abs(diff_total - sum(candidates.mapped('amount')) - sum(junk.mapped('amount'))) < 0.01
if DO_COMMIT and has_flag and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED' + ('' if has_flag else ' (falta el campo receipt_candidate)'))
