# MELANI - BARDA DEL MEDIO (ODOO-559) is the same person as MELI - BARDA DEL
# MEDIO (ODOO-433), confirmed by ale on 28/10/2026. Same steps as the Corina
# merge of 27/10: history rows move to the kept customer, the board cards
# follow, and the duplicate is archived keeping its Online ref so the sync
# never recreates it. Both balances are $0. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
P = uenv['res.partner'].sudo().with_context(active_test=False)
keep, dup = P.browse(315), P.browse(313)
assert keep.ref == 'ODOO-433' and dup.ref == 'ODOO-559'
before = keep._distri_account_balance() + dup._distri_account_balance()
rows = uenv['distri.cuenta.movimiento'].sudo().with_context(active_test=False).search([('partner_id', '=', dup.id)])
cards = uenv['distri.mp.collection'].sudo().with_context(active_test=False).search([('partner_id', '=', dup.id)])
print('CARDS', [(c.id, str(c.date)[:10], c.amount, c.state) for c in cards])
if any(c.state == 'draft' for c in cards):
    raise SystemExit('ABORT: tarjeta pendiente del duplicado')
rows.write({'partner_id': keep.id})
cards.write({'partner_id': keep.id})
dup.write({'active': False, 'comment': (dup.comment or '') +
           '<p>Duplicado de MELI - BARDA DEL MEDIO (ODOO-433): unificado en el lab el 28/10/2026.</p>'})
keep.message_post(body='Se unificó el duplicado MELANI - BARDA DEL MEDIO (ODOO-559): %d movimientos de la '
                       'historia y %d cobros de MP del tablero. Falta unificarlo en Online.' % (len(rows), len(cards)))
uenv['distri.balance.review'].sudo()._distri_refresh(keep | dup)
env.flush_all()
after = keep._distri_account_balance()
print('RESULT historial=%d tarjetas=%d saldo_antes=%.2f saldo_despues=%.2f dup_activo=%s' % (
    len(rows), len(cards), before, after, dup.active))
if DO_COMMIT and abs(after - before) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
