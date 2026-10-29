# Step 15d of the 24/10/2026 cross (odoo shell body, run by
# paso15d-martin-diletti.sh).
#
# ale confirmed on 26/10/2026 that «Martin», Luciano Martín Diletti (CUIT
# 20289479296), is only a reseller, unrelated to customer ODOO-528 «X - DANIEL
# MORALES». The module had learned that CUIT for ODOO-528 on 29/09 from
# amount and day alone; no Online receipt of ODOO-528 is tied to one of his
# transfers by the cross. So, as step 11 did for the resellers:
# - the learned identity CUIT -> ODOO-528 is removed
# - a payer rule «Re Venta Martin» (his future transfers are set aside on import)
# - his 19 pending transfers are set aside with that rule
# Guards (ABORT, nothing written): the identity not the system-learned one
# for ODOO-528, a customer still owning the CUIT, cards not the 19 expected or
# with a receipt / promise / payment, a payment created or a customer balance
# changed. Idempotent. Rolls back unless DO_COMMIT=1.
import json
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CUIT = '20289479296'
WRONG_REF = 'ODOO-528'
LABEL = 'Re Venta Martin'
MARK = 'Revisión del 26/10/2026'
REASON = ('%s: ale confirmó que Luciano Martín Diletti (CUIT 20289479296, «Martin») es solo '
          'revendedor, sin relación con ODOO-528 «X - DANIEL MORALES». Su saldo lo lleva la '
          'gerencia; la transferencia no se imputa a un cliente directo.' % MARK)
EXPECTED_COUNT = 19
EXPECTED_TOTAL = 17012000.00


def abort(msg):
    env.cr.rollback()
    print('ABORT', msg)
    print('NOT COMMITTED (aborted)')
    raise SystemExit(0)


Partner = env['res.partner'].sudo()
Collection = env['distri.mp.collection'].sudo()
Identity = env['distri.payer.identity'].sudo()
Rule = env['distri.mp.wholesale.rule'].sudo().with_context(active_test=False)

payments_before = env['account.payment'].search_count([])
receivable = [('account_id.account_type', '=', 'asset_receivable'), ('parent_state', '=', 'posted')]
balance_before = sum(env['account.move.line'].sudo().search(receivable).mapped('balance'))
stats = {}

# 1. the identity learned on 29/09
found = Identity.search([('kind', '=', 'cuit'), ('value', '=', CUIT)])
if found:
    if (len(found) != 1 or found.partner_id.commercial_partner_id.ref != WRONG_REF
            or found.create_uid.login != '__system__'):
        abort('la identidad del CUIT %s no es la aprendida sola para %s' % (CUIT, WRONG_REF))
    found.partner_id.commercial_partner_id.message_post(body=(
        '%s: se quita el CUIT %s (Luciano Martín Diletti) de los pagadores conocidos de este '
        'cliente. Se había aprendido solo por importe y día el 29/09; es un revendedor.' % (MARK, CUIT)))
    found.unlink()
    stats['identidad_quitada'] = True
if Partner._distri_partners_by_cuit(CUIT):
    abort('el CUIT sigue siendo de un cliente: %s'
          % Partner._distri_partners_by_cuit(CUIT).mapped('ref'))

# 2. the payer rule
rule = Rule.search([('cuit', '=', CUIT)])
if not rule:
    rule = Rule.create({'cuit': CUIT, 'label': LABEL,
                        'note': 'Revendedor, confirmado por ale el 26/10/2026.'})
    rule.message_post(body=REASON)
    stats['regla'] = 'creada'
elif not rule.active or rule.conflict:
    abort('la regla del CUIT existe archivada o en conflicto (%s)' % rule.id)
else:
    stats['regla'] = 'existente'

# 3. the pending transfers
pending = Collection.search([('kind', '=', 'income'), ('state', '=', 'draft'), ('payer_cuit', '=', CUIT)])
if pending:
    if len(pending) != EXPECTED_COUNT or abs(sum(pending.mapped('amount')) - EXPECTED_TOTAL) > 0.01:
        abort('pendientes del CUIT: %s por %s, se esperaban %s por %s' % (
            len(pending), sum(pending.mapped('amount')), EXPECTED_COUNT, EXPECTED_TOTAL))
    if pending.filtered(lambda c: c.receipt_id or c.promise_id or c.payment_id):
        abort('alguna tarjeta tiene foto, promesa o pago')
    moved = pending._distri_set_aside(rule=rule, label=LABEL)
    if len(moved) != len(pending):
        abort('no se apartaron todas: %s de %s' % (len(moved), len(pending)))
    for card in moved:
        card.message_post(body=REASON)
    stats['apartadas'] = len(moved)
else:
    stats['apartadas'] = 0

if env['account.payment'].search_count([]) != payments_before:
    abort('se creó un pago')
if abs(sum(env['account.move.line'].sudo().search(receivable).mapped('balance')) - balance_before) > 0.01:
    abort('cambió el saldo de clientes')

stats['tarjetas_del_cuit'] = {s: Collection.search_count([('kind', '=', 'income'), ('payer_cuit', '=', CUIT),
                                                          ('state', '=', s)])
                              for s in ('draft', 'apartado', 'confirmed', 'ignored')}
print('RESULT', json.dumps(stats, ensure_ascii=False))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
