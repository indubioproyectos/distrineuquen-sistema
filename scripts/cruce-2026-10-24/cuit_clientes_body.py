# Step 9 of the 24/10/2026 cross (odoo shell body, run by paso9-cuit-clientes.sh).
#
# The CUIT of the customers whose name carries their DNI (zona Zapala,
# «Z - … DNI …»), read from cuit_por_dni.csv: prefix 20/27/23/24 + DNI + check
# digit found in the ARCA padrón of 26/09/2026 with the same name (general
# audit of 25/10/2026, auditoria/general/arca_ideas.md). The lab customer gets
# identification type CUIT and that number; a DNI already in the field is
# replaced (it is the same person). Every customer gets a chatter note
# «CUIT desde el padrón de ARCA (paso 9, 25/10/2026): …» (once: the note is
# the idempotency mark). Nothing is merged and Online is not touched.
# Left out: 518 and 758, whose CUIT also pays for other customers (blind
# check 25/10): the module would propose them for those transfers.
# Effect on the board: a CUIT on the customer makes the module propose that
# customer for the transfers of that CUIT (cobros.py _distri_partners_by_cuit,
# after the learned identities). The run lists the board cards and learned
# identities of these CUITs so they can be checked; it changes no card.
# Guards (ABORT, nothing written): a customer not found or not unique, a VAT
# that is neither empty nor the DNI, a CUIT already on another customer, any
# change in payments, board cards or customer balances.
# Rolls back unless DO_COMMIT=1.
import csv
import json
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IN = os.environ.get('IN_DIR', '/cruce')
MARK = 'CUIT desde el padrón de ARCA (paso 9, 25/10/2026)'
EXPECTED = 19  # 518 and 758 left out: their CUIT also pays for other customers

Partner = env['res.partner'].sudo().with_context(active_test=False)
Collection = env['distri.mp.collection'].sudo()
Identity = env['distri.payer.identity'].sudo()
Message = env['mail.message'].sudo()
cuit_type = env.ref('l10n_ar.it_cuit')


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


def log(**data):
    print('LOG', json.dumps(dict(paso=9, **data), ensure_ascii=False, default=str))


def warn(**data):
    print('WARN', json.dumps(data, ensure_ascii=False, default=str))


def digits(value):
    return ''.join(ch for ch in (value or '') if ch.isdigit())


def noted(record):
    return bool(Message.search_count([('model', '=', record._name), ('res_id', '=', record.id),
                                      ('body', 'ilike', MARK)]))


def check_digit(base):
    weights = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    rest = 11 - sum(int(a) * b for a, b in zip(base, weights)) % 11
    return {11: '0', 10: None}.get(rest, str(rest))


def summary_sql(query):
    env.flush_all()
    env.cr.execute(query)
    return sorted(env.cr.fetchall())


def totals():
    return (summary_sql('select state, count(*), sum(amount) from account_payment group by 1'),
            summary_sql("select state, count(*), sum(amount) from distri_mp_collection group by 1"))


with open(os.path.join(IN, 'cuit_por_dni.csv'), encoding='utf-8') as handle:
    rows = list(csv.DictReader(handle))
if len(rows) != EXPECTED or len({r['online_id'] for r in rows}) != EXPECTED:
    abort('cuit_por_dni.csv no tiene los %s clientes' % EXPECTED)

before = totals()
all_partners = Partner.search([('ref', '=like', 'ODOO-%'), ('active', '=', True)]).filtered(
    lambda p: p == p.commercial_partner_id)
balances_before = {p.id: p._distri_account_balance() for p in all_partners}
done = {'cuit_cargado': 0, 'ya_corregido': 0, 'dni_reemplazado': 0}

for row in rows:
    cuit, dni = row['cuit'], row['dni']
    if len(cuit) != 11 or cuit[2:10] != dni or cuit[:2] not in ('20', '23', '24', '27') or \
            check_digit(cuit[:10]) != cuit[10]:
        abort('el CUIT %s no sale del DNI %s' % (cuit, dni))
    if int(row['coinciden']) < 2:
        abort('el nombre de ARCA no coincide con el cliente %s' % row['online_id'])
    found = Partner.search([('ref', '=', 'ODOO-%s' % row['online_id'])])
    partner = found.commercial_partner_id
    if len(partner) != 1 or not partner.active:
        abort('el cliente ODOO-%s no está o no es único en el lab' % row['online_id'])
    if dni.lstrip('0') not in partner.name.replace('.', ''):
        abort('el nombre del cliente ODOO-%s no tiene el DNI %s' % (row['online_id'], dni))
    others = Partner.search([('id', '!=', partner.id), ('active', '=', True)]).filtered(
        lambda p: digits(p.vat) == cuit)
    if others:
        abort('el CUIT %s ya está en otro cliente del lab: %s' % (cuit, others.ids))
    current = digits(partner.vat)
    if current == cuit and partner.l10n_latam_identification_type_id == cuit_type:
        done['ya_corregido'] += 1
        continue
    if current and current.lstrip('0') != dni.lstrip('0'):
        abort('el cliente ODOO-%s ya tiene otro número (%s)' % (row['online_id'], partner.vat))
    partner.write({'l10n_latam_identification_type_id': cuit_type.id, 'vat': cuit})
    if digits(partner.vat) != cuit:
        abort('el cliente ODOO-%s no quedó con el CUIT %s' % (row['online_id'], cuit))
    status = 'responsable inscripto en IVA' if row['iva'] == 'AC' else (
        'monotributo categoría %s' % row['mono'] if row['mono'] not in ('', 'NI') else 'inscripto')
    if not noted(partner):
        partner.message_post(body='%s: CUIT %s completado desde el DNI del nombre y el padrón de ARCA '
                                  'del 26/09/2026 (%s, %s)%s.' % (
                                      MARK, cuit, row['arca_nombre'], status,
                                      '; antes tenía cargado el DNI %s' % current if current else ''))
    done['cuit_cargado'] += 1
    done['dni_reemplazado'] += bool(current)
    # what the board and the learned identities say about this CUIT
    identities = Identity.search([('kind', '=', 'cuit'), ('value', '=', cuit)])
    alien = identities.filtered(lambda i: i.partner_id.commercial_partner_id != partner)
    if alien:
        warn(motivo='identidad aprendida para otro cliente: el módulo propone ese, no este',
             cliente=row['online_id'], cuit=cuit,
             otros=[i.partner_id.commercial_partner_id.ref for i in alien])
    cards = Collection.search([('payer_cuit', '=', cuit)])
    if cards:
        log(cliente=row['online_id'], cuit=cuit, tarjetas={
            state: len(cards.filtered(lambda c: c.state == state)) for state in set(cards.mapped('state'))},
            de_otro_cliente=[(c.source_id, c.state, c.partner_id.commercial_partner_id.ref) for c in cards
                             if c.partner_id and c.partner_id.commercial_partner_id != partner])
    rules = env['distri.mp.wholesale.rule'].sudo().search([('cuit', '=', cuit)])
    if rules:
        warn(motivo='el CUIT tiene una regla de mayorista: queda en conflicto', cliente=row['online_id'],
             cuit=cuit, regla=rules.ids)

after = totals()
if after != before:
    abort('cambiaron pagos o tarjetas: antes %s, después %s' % (before, after))
for partner in all_partners:
    if abs(partner._distri_account_balance() - balances_before[partner.id]) > 0.01:
        abort('cambió el saldo del cliente %s' % partner.id)

print('RESULT', json.dumps(dict(done, pagos_y_tablero=after), ensure_ascii=False, default=str))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
