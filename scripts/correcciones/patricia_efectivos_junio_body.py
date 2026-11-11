# RODRIGUEZ PATRICIA ALEJANDRA: two customers with the same name (ale,
# 30/10/2026). ODOO-231 (Cabellera del Frío 1485, pays from «Mini Market
# Terrazas») had two June cash payments of Simon that belong to ODOO-134
# (Huevería Collon Cura, pays from Miriam Blasco): 09/06 $10.700 (with the
# $150.000 transfer of that night, invoice 03349 $160.700) and 17/06
# $101.050 (invoice 03517, exact). The lab's copy of the history moves them
# to ODOO-134 (Online is fixed by hand) and the 09/06 $150.000 transfer
# (card 24015) is approved without receipt. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
P = uenv['res.partner'].sudo()
terrazas = P.search([('ref', '=', 'ODOO-231')])
hueveria = P.search([('ref', '=', 'ODOO-134')])
assert len(terrazas) == 1 and len(hueveria) == 1
Mov = uenv['distri.cuenta.movimiento'].sudo()
rows = Mov.browse()
for day, amount in (('2026-06-09', 10700.0), ('2026-06-17', 101050.0)):
    r = Mov.search([('partner_id', '=', terrazas.id), ('tipo', '=', 'pago'), ('fecha', '=', day),
                    ('haber', '>=', amount - 0.5), ('haber', '<=', amount + 0.5)])
    assert len(r) == 1, (day, r.mapped('comprobante'))
    rows |= r
card = uenv['distri.mp.collection'].sudo().browse(24015)
assert card.state == 'draft' and card.partner_id == hueveria and abs(card.amount - 150000) < 0.01
b_t, b_h = terrazas._distri_account_balance(), hueveria._distri_account_balance()
names = rows.mapped('comprobante')
for r in rows:
    r.write({'partner_id': hueveria.id,
             'concepto': '%s · pasado de ODOO-231 a ODOO-134 en el lab el 30/10/2026' % (r.concepto or '')})
    mirror = uenv['distri.online.payment'].sudo().search([('name', '=', r.comprobante)])
    if len(mirror) == 1 and 'partner_id' in mirror._fields:
        mirror.write({'partner_id': hueveria.id})
uenv['distri.mp.vouch'].sudo().create({'collection_id': card.id, 'note':
    'Lo confirmó ale el 30/10/2026: transferencia desde la cuenta de Miriam Blasco; con el efectivo de $10.700 '
    'paga la factura 03349 del 09/06'}).action_apply()
for p, text in ((terrazas, 'Los efectivos %s (09/06 $ 10.700 y 17/06 $ 101.050) eran de RODRIGUEZ PATRICIA ALEJANDRA - '
                           'HUEVERIA COLLON CURA (ODOO-134); se pasaron a esa cuenta (30/10/2026).'),
                (hueveria, 'Se pasaron desde ODOO-231 los efectivos %s (09/06 $ 10.700 y 17/06 $ 101.050) y se aprobó '
                           'sin comprobante la transferencia de $ 150.000 del 09/06 (30/10/2026).')):
    p.message_post(body=text % ', '.join(names))
uenv['distri.balance.review'].sudo()._distri_refresh(terrazas | hueveria)
env.flush_all()
a_t, a_h = terrazas._distri_account_balance(), hueveria._distri_account_balance()
print('RESULT', names, card.payment_id.name, '| 231 %.2f -> %.2f | 134 %.2f -> %.2f' % (b_t, a_t, b_h, a_h))
if DO_COMMIT and abs(a_t - b_t - 111750) < 0.01 and abs(b_h - a_h - 261750) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
