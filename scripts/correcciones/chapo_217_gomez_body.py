# MP 151998276694 (26/03/2026 12:38, $217.160) is CHAPOCHNICOFF SANDRO's
# (ODOO-21): the photo Italo sent at 12:39 shows his CUIT, but the caption said
# «GÓMEZ JULIO», so Online has it as PBNK1/2026/00588 for GOMEZ JULIO
# (ODOO-74). On 04/06 Italo resent the same photo as «CHAPOCHNIKOFF SANDRO»
# and Online loaded it again as PBNK1/2026/01721 for Chapo. One transfer, two
# Online payments: the card moves to Chapo and links to 01721; 00588 stays
# unlinked until the administration cancels it in Online. The photo resent on
# 23/06 (IMG-20260623-WA0005, Chapo's 04/06 $400.000) inherited «VIDEA RAMIRO»
# from the previous caption. No balance changes. Reviewed with ale on
# 29/10/2026. Rolls back unless DO_COMMIT=1.
import os
from odoo import fields

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
P = uenv['res.partner'].sudo()
chapo, gomez = P.browse(97), P.browse(194)
assert chapo.ref == 'ODOO-21' and gomez.ref == 'ODOO-74'
OP = uenv['distri.online.payment'].sudo()
op_gomez = OP.search([('name', '=', 'PBNK1/2026/00588')])
op_chapo = OP.search([('name', '=', 'PBNK1/2026/01721')])
assert op_gomez.partner_id == gomez and op_chapo.partner_id == chapo and not op_chapo.collection_id
card = uenv['distri.mp.collection'].sudo().search([('source_id', '=', '151998276694'), ('kind', '=', 'income')])
assert len(card) == 1 and card.online_payment_id == op_gomez and card.partner_id == gomez
Msg = uenv['distri.whatsapp.message'].sudo()
photo_mar, photo_jun = Msg.browse(10885), Msg.browse(12066)
assert photo_mar.media_filename == 'IMG-20260326-WA0063.jpg' and photo_jun.media_filename == 'IMG-20260623-WA0005.jpg'
balances = {p.id: p._distri_account_balance() for p in chapo | gomez}

card.action_online_unlink()
card.write({'partner_id': chapo.id, 'online_unlinked': False})
op_chapo.write({'collection_id': card.id, 'link_kind': 'amount', 'linked_at': fields.Datetime.now()})
card._distri_mark_online(op_chapo)
card.message_post(body='Es de CHAPOCHNICOFF SANDRO: la foto de Italo (26/03 12:39) muestra su CUIT. '
                       'Online la tiene dos veces: PBNK1/2026/01721 (Chapo, correcto) y PBNK1/2026/00588 '
                       '(GOMEZ JULIO, por la leyenda equivocada; falta anularlo en Online).')
photo_jun.write({'distri_partner_id': chapo.id})
if photo_mar.distri_partner_id != chapo:
    photo_mar.write({'distri_partner_id': chapo.id})
for partner in chapo | gomez:
    partner.message_post(body='Transferencia de $217.160 del 26/03/2026 (MP 151998276694) cargada dos veces en '
                              'Online: PBNK1/2026/01721 a CHAPOCHNICOFF SANDRO (correcto) y PBNK1/2026/00588 a '
                              'GOMEZ JULIO (error). Revisado el 29/10/2026.')
uenv['distri.balance.review'].sudo()._distri_refresh(chapo | gomez)
env.flush_all()
after = {pid: P.browse(pid)._distri_account_balance() for pid in balances}
print('RESULT tarjeta=%s %s %s %s | mensajes=%s,%s | saldos %s -> %s' % (
    card.partner_id.name, card.state, card.ignore_reason, card.online_payment_id.name,
    photo_mar.distri_partner_id.id, photo_jun.distri_partner_id.id, balances, after))
ok = all(abs(balances[k] - after[k]) < 0.01 for k in balances)
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED', 'saldos ok' if ok else 'SALDOS CAMBIARON')
