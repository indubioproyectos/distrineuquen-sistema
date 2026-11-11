# CAMACHO MARIA DE LOS ANGELES (ODOO-437), ale 30/10/2026: the two $75.400
# MP transfers of 20/02 are both real (operations 146406013251 18:56 and
# 146425049403 20:52, report of February). PBNK1/2026/00863 in Online is the
# 18:56 one (loaded on 15/04 with the receipt Simon re-sent), not the 20:52
# one the old cross paired by amount. So: PMP/2026/00636 (18:56) is undone
# and its card discarded as already in Online; the 20:52 one goes back to
# the board unconfirmed, to review with the administrator first.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
C = uenv['distri.mp.collection']
partner = uenv['res.partner'].sudo().search([('ref', '=', 'ODOO-437')])
photo, other = C.browse(45946), C.browse(45941)
assert photo.state == 'confirmed' and photo.payment_id.name == 'PMP/2026/00636' and photo.source_id == '146406013251'
assert other.state == 'ignored' and other.source_id == '146425049403' and other.partner_id == partner
before = partner._distri_account_balance()
photo.action_undo()
uenv['distri.mp.ignore'].create({
    'collection_id': photo.id, 'reason': 'other',
    'note': 'Ya en Online: PBNK1/2026/00863 (cargado el 15/04 con este comprobante, que Simon reenvió); '
            'PMP/2026/00636 era doble (30/10/2026)',
}).action_ignore()
other.action_online_unlink()
assert other.state == 'draft' and not other.online_payment_id, (other.state, other.online_payment_id)
# a candidate waits in «Revisar»: never confirmed on sight, nor by «Confirmar todos»
other.sudo().write({'partner_id': partner.id, 'receipt_candidate': True,
                    'proposal_note': 'A revisar con Administración: segunda transferencia de $75.400 del 20/02 '
                                     '(20:52), no está en Online. No se confirma hasta verlo (30/10/2026).'})
other.sudo().message_post(body='Vuelve al tablero sin confirmar: PBNK1/2026/00863 es la transferencia de las '
                               '18:56, no esta. Se ve primero con el administrador (30/10/2026).')
(photo | other).sudo()._migration_recompute()
uenv['distri.balance.review'].sudo()._distri_refresh(partner)
env.flush_all()
after = partner._distri_account_balance()
print('RESULT foto', photo.state, '| otra', other.state, other.column, other.migration_box, '| saldo %.2f -> %.2f' % (before, after))
if DO_COMMIT and photo.state == 'ignored' and other.state == 'draft' and other.column == 'review' and abs(after - before - 75400) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
