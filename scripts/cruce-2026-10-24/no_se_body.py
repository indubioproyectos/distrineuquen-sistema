# Step 12 of the 24/10/2026 cross (odoo shell body, run by paso12-no-se.sh).
#
# From the review of the 93 payers the manager left as «No sé»
# (auditoria/no_se/informe.md, 25/10/2026):
# 1. Payer identities the module learned on 29/09 from round-amount matches
#    (paso12_identidades.csv, all created by __system__): the Online credits
#    they rest on are other payers' receipts, so the module proposes the wrong
#    customer for every pending card of those CUITs. Removed, with a note on
#    the customer, and those cards proposed again.
# 2. Transfers of payers whose receipts in the WhatsApp chats name a retail
#    customer (paso12_casos.csv): confirmed for that customer through the
#    module, each with a note «Revisión de pagadores del 25/10/2026: …».
#    A customer that would end in credit is skipped whole (left for review),
#    as is a card that is not pending. Identities learned by these confirms are
#    removed again (step 10's lesson: a new identity re-proposes every
#    pending card of that CUIT).
# Guards (ABORT, nothing written): an identity or card not as expected,
# payments or board totals that do not move by exactly the loads, any
# customer balance that does not move by exactly its loads.
# Rolls back unless DO_COMMIT=1.
import csv
import json
import os
from collections import Counter, defaultdict

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IN = os.environ.get('IN_DIR', '/cruce')
MARK = 'Revisión de pagadores del 25/10/2026'

Partner = env['res.partner'].sudo().with_context(active_test=False)
Payment = env['account.payment'].sudo()
Collection = env['distri.mp.collection'].sudo()
Identity = env['distri.payer.identity'].sudo()
Message = env['mail.message'].sudo()


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


def log(**data):
    print('LOG', json.dumps(dict(paso=12, **data), ensure_ascii=False, default=str))


def skip(**data):
    print('SKIP', json.dumps(data, ensure_ascii=False, default=str))


def noted(record):
    return bool(Message.search_count([('model', '=', record._name), ('res_id', '=', record.id),
                                      ('body', 'ilike', MARK)]))


def read(name):
    with open(os.path.join(IN, name), encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def summary_sql(query):
    env.flush_all()
    env.cr.execute(query)
    return {row[0]: [row[1], round(float(row[2] or 0), 2)] for row in env.cr.fetchall()}


def lab_partner(ref):
    found = Partner.search([('ref', '=', ref)]).commercial_partner_id
    if len(found) != 1 or not found.active:
        abort('cliente %s: no está o no es único' % ref)
    return found


wrong = read('paso12_identidades.csv')
loads = read('paso12_casos.csv')
if len(wrong) != 8 or len(loads) != 6:
    abort('los archivos del paso 12 no tienen 8 identidades y 6 operaciones')

payments_before = summary_sql('select state, count(*), sum(amount) from account_payment group by 1')
cards_before = summary_sql("select state, count(*), sum(amount) from distri_mp_collection "
                           "where kind = 'income' group by 1")
all_partners = Partner.search([('ref', '=like', 'ODOO-%'), ('active', '=', True)]).filtered(
    lambda p: p == p.commercial_partner_id)
balances_before = {p.id: p._distri_account_balance() for p in all_partners}
stats = Counter()

# ------------------------------------------------------------------ 1
cuits = []
for row in wrong:
    found = Identity.browse(int(row['identidad'])).exists()
    if not found:
        stats['identidad_ya_quitada'] += 1
        continue
    if found.kind != 'cuit' or found.value != row['cuit'] or \
            found.partner_id.commercial_partner_id.ref != row['cliente'] or found.create_uid.login != '__system__':
        abort('la identidad %s no es la aprendida sola del CUIT %s para %s' % (row['identidad'], row['cuit'], row['cliente']))
    customer = found.partner_id.commercial_partner_id
    customer.message_post(body=(
        '%s: se quita el CUIT %s de los pagadores conocidos de este cliente. Se había aprendido solo '
        'por importe redondo el 29/09, y el cobro de Online en que se apoyaba corresponde al '
        'comprobante de otro pagador.' % (MARK, row['cuit'])))
    log(accion='identidad_quitada', identidad=found.id, cuit=row['cuit'], cliente=row['cliente'])
    found.unlink()
    cuits.append(row['cuit'])
    stats['identidades_quitadas'] += 1
if cuits:
    pending = Collection.search([('kind', '=', 'income'), ('state', '=', 'draft'),
                                 ('payer_cuit', 'in', cuits), ('proposal', '!=', 'manual')])
    pending._propose()
    left = pending.filtered(lambda c: c.proposal == 'cuit' and c.payer_cuit in cuits)
    still = left.filtered(lambda c: Partner._distri_partners_by_cuit(c.payer_cuit))
    log(accion='tarjetas_repropuestas', n=len(pending),
        propuestas=dict(Counter(pending.mapped(lambda c: c.proposal or 'sin_propuesta'))),
        con_otro_cliente_por_cuit=len(still))
    stats['tarjetas_repropuestas'] = len(pending)

# ------------------------------------------------------------------ 2
by_customer = defaultdict(list)
for row in loads:
    card = Collection.search([('kind', '=', 'income'), ('source_id', '=', row['op'])])
    if len(card) != 1 or abs(card.amount - float(row['importe'])) > 0.005:
        abort('la operación %s no está en el tablero con ese importe' % row['op'])
    target = lab_partner(row['cliente'])
    if card.state == 'confirmed' and card.partner_id.commercial_partner_id == target and noted(card):
        stats['ya_cargado'] += 1
        continue
    if card.state not in ('draft', 'apartado') or card.payment_id:
        stats['skip_no_pendiente'] += 1
        skip(op=row['op'], motivo='estado %s' % card.state)
        continue
    by_customer[target].append((row, card))

identities_before = Identity.search([]).ids
expected_move = Counter()
card_delta = Counter()
created = Payment.browse()
for target, items in by_customer.items():
    total = sum(c.amount for _r, c in items)
    if balances_before.get(target.id, 0.0) - total < -0.01:
        stats['skip_queda_a_favor'] += len(items)
        skip(cliente=target.ref, motivo='quedaría a favor', saldo=round(balances_before.get(target.id, 0.0), 2),
             carga=round(total, 2), ops=[r['op'] for r, _c in items])
        continue
    for row, card in items:
        was = card.state
        if was == 'apartado':
            card._distri_bring_back(MARK)
        card.write({'partner_id': target.id, 'proposal': 'manual',
                    'proposal_note': ('%s: comprobante del chat' % MARK)[:250]})
        card.action_confirm()
        new = card.payment_id
        if card.state != 'confirmed' or not new or new.partner_id.commercial_partner_id != target or \
                abs(new.amount - card.amount) > 0.005:
            abort('la operación %s no quedó confirmada para %s' % (row['op'], target.ref))
        text = '%s: se carga a %s. %s' % (MARK, target.name, row['evidencia'])
        if not noted(card):
            card.message_post(body=text)
            new.message_post(body=text)
        created |= new
        card_delta[(was, 'n')] -= 1
        card_delta[(was, 'amount')] -= card.amount
        card_delta[('confirmed', 'n')] += 1
        card_delta[('confirmed', 'amount')] += card.amount
        expected_move[target.id] -= new.amount
        stats['cargado'] += 1
        log(accion='cargado', op=row['op'], pago=new.id, nombre=new.name, cliente=target.ref, importe=new.amount)

learned = Identity.search([('id', 'not in', identities_before)])
if learned:
    log(accion='identidades_nuevas_quitadas', identidades=[(i.kind, i.value, i.partner_id.ref) for i in learned])
    learned.unlink()

# ------------------------------------------------------------------ guards
env.flush_all()
payments_after = summary_sql('select state, count(*), sum(amount) from account_payment group by 1')
if sum(v[0] for v in payments_after.values()) - sum(v[0] for v in payments_before.values()) != len(created):
    abort('pagos nuevos distintos de los cargados')
cards_after = summary_sql("select state, count(*), sum(amount) from distri_mp_collection "
                          "where kind = 'income' group by 1")
for state in set(cards_before) | set(cards_after) | {k[0] for k in card_delta}:
    b, a = cards_before.get(state, [0, 0.0]), cards_after.get(state, [0, 0.0])
    if a[0] - b[0] != card_delta[(state, 'n')] or abs(a[1] - b[1] - card_delta[(state, 'amount')]) > 0.01:
        abort('el tablero no cambió solo por las cargas en %s: antes %s, después %s' % (state, b, a))
for partner in all_partners:
    change = partner._distri_account_balance() - balances_before[partner.id]
    if abs(change - expected_move.get(partner.id, 0.0)) > 0.01:
        abort('el saldo del cliente %s cambió %s y no %s' % (partner.ref, round(change, 2), expected_move.get(partner.id, 0.0)))

print('RESULT', json.dumps({**dict(stats), 'pagos_nuevos': len(created),
                            'importe': round(sum(created.mapped('amount')), 2),
                            'tablero_antes': cards_before, 'tablero_despues': cards_after}, ensure_ascii=False))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
