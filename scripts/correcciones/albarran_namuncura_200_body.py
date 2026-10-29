# Two crossed $200.000 links, checked by a second agent on 28/10/2026:
# - Online PBNK1/2026/04020 (29/09, ALBARRÁN) was on card 26430; it belongs to
#   card 27114 (BNA 29/09 20:15, Italo's note "ALBARRAN GUILLERMO"; the other
#   photo of that minute is Palacios' Galicia $70.000 = Online 04019). The
#   duplicate photo card 49914 also goes to Albarrán.
# - Online PBNK1/2026/03820 (18/09, JOSE RUBALDO CARO NAMUNCURA) was on card
#   26749 (10/09, no receipt); it belongs to card 26430 (BNA 19/09 09:48, note
#   "José Namuncurá"). 26749 goes back to the board without receipt.
# Same steps as relink_online.py. No balance changes. Rolls back unless DO_COMMIT=1.
import os
from odoo import fields

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
C = uenv['distri.mp.collection']
OP = uenv['distri.online.payment'].sudo()
P = uenv['res.partner'].sudo()
albarran, jose, palacios = P.browse(16), P.browse(238), P.browse(378)
assert (albarran.ref, jose.ref, palacios.ref) == ('ODOO-47', 'ODOO-89', 'ODOO-386')
c26430, c27114, c26749, c49914 = C.browse(26430), C.browse(27114), C.browse(26749), C.browse(49914)
op_alb = OP.search([('name', '=', 'PBNK1/2026/04020')])
op_jose = OP.search([('name', '=', 'PBNK1/2026/03820')])
assert op_alb.collection_id == c26430 and op_alb.partner_id == albarran
assert op_jose.collection_id == c26749 and op_jose.partner_id == jose
assert not c27114.online_payment_id and c27114.amount == c26430.amount == c26749.amount == 200000
balances = {p.id: p._distri_account_balance() for p in albarran | jose | palacios}


def free(card):
    card.action_online_unlink()
    if card.receipt_id and (card.receipt_id.distri_receipt_note or '').startswith('Ya cargado en Online'):
        card.receipt_id.sudo().with_context(distri_receipt_matching=True).write({'distri_receipt_note': False})


def attach(card, op, partner, why):
    card.sudo().write({'partner_id': partner.id})
    op.write({'collection_id': card.id, 'linked_at': fields.Datetime.now()})
    if card.state == 'draft':
        card.sudo()._distri_mark_online(op)
    else:
        card.sudo().write({'online_payment_id': op.id, 'ignore_reason': 'online',
                           'ignore_note': 'Ya cargado en Online: %s' % op.name})
    card.sudo().message_post(body='Vínculo con Online corregido: %s, cliente %s. %s' % (op.name, partner.name, why))


# 1. Albarrán 29/09
free(c26430)
attach(c27114, op_alb, albarran, 'Comprobante BNA del 29/09 20:15 con la nota de Italo "ALBARRAN GUILLERMO"; '
       'el otro comprobante de ese minuto es el Galicia de Palacios ($70.000, PBNK1/2026/04019).')
c49914.sudo().write({'partner_id': albarran.id})
c49914.sudo().message_post(body='Cliente corregido: PALACIOS MERCEDES → %s (es la misma foto que %s).'
                                % (albarran.name, c27114.name))
# 2. José Namuncurá 19/09
free(c26749)
attach(c26430, op_jose, jose, 'Comprobante BNA del 19/09 09:48 con la nota "José Namuncurá".')
c26749.sudo().write({'partner_id': False})
c26749.sudo().message_post(body='El pago de Online %s pasó a %s (comprobante del grupo): esta transferencia '
                                'vuelve al tablero sin comprobante.' % (op_jose.name, c26430.name))
(c26430 | c27114 | c26749 | c49914).sudo()._migration_recompute()
env.flush_all()
after = {pid: P.browse(pid)._distri_account_balance() for pid in balances}
for c in (c27114, c26430, c26749, c49914):
    print('CARD', c.id, c.state, c.partner_id.name, c.online_payment_id.name, c.migration_box)
print('SALDOS', balances, after)
ok = all(abs(balances[k] - after[k]) < 0.01 for k in balances)
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED', 'saldos ok' if ok else 'SALDOS CAMBIARON')
