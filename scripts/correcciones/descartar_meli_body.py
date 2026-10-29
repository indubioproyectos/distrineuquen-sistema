# MELI - BARDA DEL MEDIO (lab partner 315): its account is at $0, checked by
# ale on 28/10/2026, so the pending «Distribuidora Aida» transfers (CUIT
# 27434881744, payer RIVAS MELANI CAROLINA) are already counted. Discards
# them like the «Descartar» wizard (reason «other») and records the CUIT on
# the partner so future transfers are recognised. Also prints the partner
# «MELANI - BARDA DEL MEDIO» (same person) for the merge decision.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CUIT = '27434881744'
NOTE = 'MELI - BARDA DEL MEDIO: cuenta en $0 verificada (28/10/2026)'
P = env['res.partner'].sudo().with_context(active_test=False)
C = env['distri.mp.collection'].sudo()
meli = P.browse(315)
assert meli.name == 'MELI - BARDA DEL MEDIO'
before = meli._distri_account_balance()
cards = C.search([('state', '=', 'draft'), ('partner_id', '=', False), '|', ('payer_cuit', 'ilike', CUIT),
                  ('payer_name', '=ilike', 'Distribuidora Aida')])
for card in cards:
    card.write({'state': 'ignored', 'ignore_reason': 'other', 'ignore_note': NOTE})
    if card.kind in ('receipt', 'verify') and card.receipt_id:
        card.receipt_id.distri_receipt_state = 'dismissed'
    card.message_post(body='Descartado por %s: Otro motivo. %s' % (env.user.name, NOTE))
other = P.search([('vat', 'ilike', CUIT), ('id', '!=', meli.id)])
if other:
    print('WARN cuit ya en', [(p.id, p.name) for p in other])
elif not meli.vat:
    meli.vat = CUIT
after = meli._distri_account_balance()
print('RESULT tarjetas=%d importe=%.2f vat=%s saldo_antes=%.2f saldo_despues=%.2f' % (
    len(cards), sum(cards.mapped('amount')), meli.vat, before, after))
print('IDS', ','.join(map(str, cards.ids)))
for p in P.search([('name', 'ilike', 'MELANI'), ('name', 'ilike', 'BARDA')]):
    n_c = env['distri.mp.collection'].sudo().with_context(active_test=False).search_count([('partner_id', '=', p.id)])
    n_m = env['distri.cuenta.movimiento'].sudo().search_count([('partner_id', '=', p.id)])
    n_a = env['account.move.line'].sudo().search_count([('partner_id', '=', p.id)])
    print('MELANI', p.id, p.name, '| ref', p.ref, '| vat', p.vat, '| saldo %.2f' % p._distri_account_balance(),
          '| tarjetas', n_c, '| historial', n_m, '| asientos', n_a, '| activo', p.active)
if DO_COMMIT and after == before:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
