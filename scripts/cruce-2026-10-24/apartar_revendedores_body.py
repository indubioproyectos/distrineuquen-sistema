# Step 11 of the 24/10/2026 cross (odoo shell body, run by paso11-revendedores.sh).
#
# The payers the manager classified on 25/10/2026 as «Revendedor o
# distribuidora» or «Cliente de un revendedor o distribuidora» (page
# «Clasificación de clientes por listas de precios»), read from
# paso11_revendedores.csv. Resellers' balances are kept by the manager and set
# at migration, so their transfers leave the retail matching:
# - a payer with a CUIT gets a payer rule (distri.mp.wholesale.rule): its
#   future transfers are set aside on import. A «cliente de revendedores» gets
#   the generic label «Cliente de revendedores (varios)»: a shop may pay on
#   behalf of several resellers, so it is never tied to one;
# - its pending transfers (draft) are set aside with that label; a bank
#   transfer has no CUIT, so its operations come listed in the file (matched
#   by the account-statement name) and are set aside once, with no rule.
# Left alone (SKIP): a CUIT that is a customer's (identity or VAT: the rule
# would be «En conflicto»), and a card with a receipt, a promise or a customer
# chosen other than by the payer's CUIT (retail signals: someone tied it to a
# customer). Three identities learned from amount and day alone
# (WRONG_IDENTITIES) are removed first. A bank payer has no rule: its cards
# go back one by one («Volver al tablero»). Cards already confirmed, ignored or
# set aside are not touched. Every card and rule gets a chatter note
# «Clasificación del gerente del 25/10/2026: …» (once: the idempotency mark).
# Guards (ABORT, nothing written): payments or any customer balance change,
# board totals that do not move by exactly the cards set aside.
# Rolls back unless DO_COMMIT=1.
import csv
import json
import os
from collections import Counter

from odoo.addons.distri_pedido.models.mayoristas import APARTADO, normalize_cuit

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IN = os.environ.get('IN_DIR', '/cruce')
MARK = 'Clasificación del gerente del 25/10/2026'
EXPECTED = 23
# payer CUIT -> customer the module learned on 29/09 from amount and day only
# (precargar_cuit_body.py), with no Online credit behind it: the manager says
# the payer is a reseller (blind check 25/10). Removed before the rule.
WRONG_IDENTITIES = {'27289476046': 'ODOO-124', '20384918221': 'ODOO-19', '20176047659': 'ODOO-20'}

Partner = env['res.partner'].sudo().with_context(active_test=False)
Collection = env['distri.mp.collection'].sudo()
Rule = env['distri.mp.wholesale.rule'].sudo().with_context(active_test=False)
Message = env['mail.message'].sudo()
Identity = env['distri.payer.identity'].sudo()


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


def log(**data):
    print('LOG', json.dumps(dict(paso=11, **data), ensure_ascii=False, default=str))


def skip(**data):
    print('SKIP', json.dumps(data, ensure_ascii=False, default=str))


def noted(record):
    return bool(Message.search_count([('model', '=', record._name), ('res_id', '=', record.id),
                                      ('body', 'ilike', MARK)]))


def summary_sql(query):
    env.flush_all()
    env.cr.execute(query)
    return {row[0]: [row[1], round(float(row[2] or 0), 2)] for row in env.cr.fetchall()}


with open(os.path.join(IN, 'paso11_revendedores.csv'), encoding='utf-8') as handle:
    rows = list(csv.DictReader(handle))
if len(rows) != EXPECTED or any(r['tipo'] not in ('mayorista', 'de_mayorista') for r in rows):
    abort('paso11_revendedores.csv no tiene los %s pagadores clasificados' % EXPECTED)

payments_before = summary_sql('select state, count(*), sum(amount) from account_payment group by 1')
cards_before = summary_sql("select state, count(*), sum(amount) from distri_mp_collection "
                           "where kind = 'income' group by 1")
all_partners = Partner.search([('ref', '=like', 'ODOO-%'), ('active', '=', True)]).filtered(
    lambda p: p == p.commercial_partner_id)
balances_before = {p.id: p._distri_account_balance() for p in all_partners}
stats = Counter()
moved_total = Collection.browse()

for cuit, ref in WRONG_IDENTITIES.items():
    found = Identity.search([('kind', '=', 'cuit'), ('value', '=', cuit)])
    if not found:
        stats['identidad_ya_quitada'] += 1
        continue
    if len(found) != 1 or found.partner_id.commercial_partner_id.ref != ref or found.create_uid.login != '__system__':
        abort('la identidad del CUIT %s no es la aprendida sola para %s' % (cuit, ref))
    found.partner_id.commercial_partner_id.message_post(body=(
        '%s: se quita el CUIT %s de los pagadores conocidos de este cliente. Se había aprendido solo '
        'por importe y día el 29/09 y el gerente lo clasificó como revendedor.' % (MARK, cuit)))
    log(accion='identidad_quitada', cuit=cuit, cliente=ref, identidad=found.id)
    found.unlink()
    stats['identidades_quitadas'] += 1

for row in rows:
    what = 'revendedor o distribuidora' if row['tipo'] == 'mayorista' else \
        'cliente de revendedores (puede pagar a cuenta de varios)'
    reason = '%s: el pagador %s es %s (ficha %s). Su saldo lo lleva la gerencia; la transferencia ' \
             'no se imputa a un cliente directo.' % (MARK, row['pagador'], what, row['ficha'])
    rule = Rule.browse()
    cuit = normalize_cuit(row['cuit'])
    if row['cuit'] and not cuit:
        abort('el CUIT de la ficha %s no tiene 11 números' % row['ficha'])
    if cuit:
        if Partner._distri_partners_by_cuit(cuit):
            stats['skip_cuit_de_cliente'] += 1
            skip(ficha=row['ficha'], cuit=cuit, motivo='el CUIT es de un cliente',
                 clientes=Partner._distri_partners_by_cuit(cuit).mapped('ref'))
            continue
        rule = Rule.search([('cuit', '=', cuit)])
        if not rule:
            rule = Rule.create({'cuit': cuit, 'label': row['etiqueta'],
                                'note': 'Clasificado por el gerente el 25/10/2026 (ficha %s).' % row['ficha']})
            rule.message_post(body=reason)
            stats['reglas_creadas'] += 1
        elif not rule.active or rule.conflict:
            stats['skip_regla_inactiva'] += 1
            skip(ficha=row['ficha'], cuit=cuit, motivo='la regla existe archivada o en conflicto', regla=rule.id)
            continue
        else:
            stats['reglas_existentes'] += 1
        cards = Collection.search([('kind', '=', 'income'), ('payer_cuit', '=', cuit)])
    else:
        ops = [o for o in row['ops'].split('|') if o]
        if not ops:
            abort('la ficha %s no tiene CUIT ni operaciones' % row['ficha'])
        cards = Collection.search([('kind', '=', 'income'), ('source_id', 'in', ops)])
        stats['ops_sin_tarjeta'] += len(set(ops) - set(cards.mapped('source_id')))
    todo = cards.filtered(lambda c: c.state == 'draft')
    retail = todo.filtered(lambda c: c.receipt_id or c.promise_id or c.payment_id or
                           (c.partner_id and c.proposal != 'cuit'))
    for card in retail:
        stats['skip_con_comprobante'] += 1
        skip(ficha=row['ficha'], op=card.source_id, cobro=card.id, motivo='tiene comprobante o promesa')
    todo -= retail
    others = {s: n for s, n in Counter(cards.mapped('state')).items() if s != 'draft'}
    stats['ya_apartadas'] += len(cards.filtered(lambda c: c.state == APARTADO))
    if todo:
        moved = todo._distri_set_aside(rule=rule or None, label=row['etiqueta'])
        if len(moved) != len(todo) or moved.filtered(lambda c: c.state != APARTADO):
            abort('la ficha %s no apartó todas sus transferencias' % row['ficha'])
        for card in moved:
            if not noted(card):
                card.message_post(body=reason)
        moved_total |= moved
    stats['apartadas:%s' % row['tipo']] += len(todo)
    log(ficha=row['ficha'], tipo=row['tipo'], cuit=cuit or None, regla=rule.id or None,
        apartadas=len(todo), importe=round(sum(todo.mapped('amount')), 2),
        otras=others)

# ------------------------------------------------------------------ guards
payments_after = summary_sql('select state, count(*), sum(amount) from account_payment group by 1')
if payments_after != payments_before:
    abort('cambiaron los pagos: antes %s, después %s' % (payments_before, payments_after))
cards_after = summary_sql("select state, count(*), sum(amount) from distri_mp_collection "
                          "where kind = 'income' group by 1")
n, amount = len(moved_total), round(sum(moved_total.mapped('amount')), 2)
want = {k: list(v) for k, v in cards_before.items()}
want.setdefault('draft', [0, 0.0]); want.setdefault(APARTADO, [0, 0.0])
want['draft'] = [want['draft'][0] - n, round(want['draft'][1] - amount, 2)]
want[APARTADO] = [want[APARTADO][0] + n, round(want[APARTADO][1] + amount, 2)]
for state in set(want) | set(cards_after):
    a, b = cards_after.get(state, [0, 0.0]), want.get(state, [0, 0.0])
    if a[0] != b[0] or abs(a[1] - b[1]) > 0.01:
        abort('el tablero no cambió solo por lo apartado en %s: %s y no %s' % (state, a, b))
for partner in all_partners:
    if abs(partner._distri_account_balance() - balances_before[partner.id]) > 0.01:
        abort('cambió el saldo del cliente %s' % partner.ref)

print('RESULT', json.dumps({**dict(stats), 'apartadas': n, 'importe_apartado': amount,
                            'tablero_antes': cards_before, 'tablero_despues': cards_after},
                           ensure_ascii=False))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
