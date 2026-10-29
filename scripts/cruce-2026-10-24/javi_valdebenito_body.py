# Step 15c of the 24/10/2026 cross (odoo shell body, run by
# paso15c-javi-valdebenito.sh).
#
# ale confirmed on 26/10/2026 that customer ODOO-565 «JAVI BALD» is Javier
# César Valdebenito, CUIT 23346620609, whose Mercado Pago account is named
# «Distri Victoria». He is both a retail customer (Online invoices, each paid
# the same day for the exact amount, balance 0) and a reseller whose
# purchases are invoiced in the manager's previous system. His 16 pending
# transfers match no Online invoice: they pay down the reseller account, so
# they are set aside with the same label as his other 29 («Re Venta - Javi
# Valdebenito»). No payer rule: the same CUIT also pays his retail invoices,
# so future transfers stay on the board, proposed to ODOO-565.
# - the customer gets the CUIT (vat)
# - the 16 cards: set aside through the module's _distri_set_aside
# Guards (ABORT, nothing written): customer missing or with another CUIT,
# cards not exactly the 16 expected, a payment created or a customer balance
# changed. Idempotent. Rolls back unless DO_COMMIT=1.
import json
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CUIT = '23346620609'
REF = 'ODOO-565'
LABEL = 'Re Venta - Javi Valdebenito'
NOTE = ('Revisión del 26/10/2026: ale confirmó que ODOO-565 «JAVI BALD» es Javier César '
        'Valdebenito (CUIT 23346620609, cuenta de MP «Distri Victoria»). No coincide con '
        'ninguna factura de Online: paga su cuenta de revendedor del sistema anterior.')
EXPECTED_COUNT = 16
EXPECTED_TOTAL = 4565000.00


def abort(msg):
    env.cr.rollback()
    print('ABORT', msg)
    print('NOT COMMITTED (aborted)')
    raise SystemExit(0)


Partner = env['res.partner'].sudo()
Collection = env['distri.mp.collection'].sudo()
partner = Partner.search([('ref', '=', REF), ('parent_id', '=', False)])
if len(partner) != 1:
    abort('cliente %s: %s fichas' % (REF, len(partner)))
if partner.vat and partner.vat != CUIT:
    abort('%s ya tiene otro CUIT: %s' % (REF, partner.vat))

payments_before = env['account.payment'].search_count([])
balance_before = sum(env['account.move.line'].sudo().search([
    ('account_id.account_type', '=', 'asset_receivable'),
    ('parent_state', '=', 'posted')]).mapped('balance'))

pending = Collection.search([('kind', '=', 'income'), ('state', '=', 'draft'),
                             ('payer_cuit', '=', CUIT)])
already = Collection.search_count([('kind', '=', 'income'), ('state', '=', 'apartado'),
                                   ('payer_cuit', '=', CUIT),
                                   ('message_ids.body', 'ilike', 'Revisión del 26/10/2026')])
if already and not pending:
    print('SKIP ya aplicado: %s tarjetas apartadas' % already)
else:
    if len(pending) != EXPECTED_COUNT or abs(sum(pending.mapped('amount')) - EXPECTED_TOTAL) > 0.01:
        abort('pendientes del CUIT: %s por %s, se esperaban %s por %s' % (
            len(pending), sum(pending.mapped('amount')), EXPECTED_COUNT, EXPECTED_TOTAL))
    if pending.filtered(lambda c: c.receipt_id or c.promise_id
                        or (c.partner_id and c.partner_id.commercial_partner_id != partner)):
        abort('alguna tarjeta tiene foto, promesa u otro cliente')
    done = pending._distri_set_aside(label=LABEL)
    for card in done:
        card.message_post(body=NOTE)

if not partner.vat:
    partner.write({'vat': CUIT})
    partner.message_post(body=NOTE)

if env['account.payment'].search_count([]) != payments_before:
    abort('se creó un pago')
balance_after = sum(env['account.move.line'].sudo().search([
    ('account_id.account_type', '=', 'asset_receivable'),
    ('parent_state', '=', 'posted')]).mapped('balance'))
if abs(balance_after - balance_before) > 0.01:
    abort('cambió el saldo de clientes')

summary = {s: Collection.search_count([('kind', '=', 'income'), ('payer_cuit', '=', CUIT),
                                       ('state', '=', s)])
           for s in ('draft', 'apartado', 'confirmed', 'ignored')}
print('RESULT', json.dumps({'cliente': partner.name, 'cuit': partner.vat, 'tarjetas_del_cuit': summary},
                           ensure_ascii=False))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
