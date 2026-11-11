# «Aixa» (CUIT 27-40899472-7): 9 pending MP transfers (05-10/2026,
# $1.162.190) proposed for SANDOVAL JAQUELINE - DESPENSA XIOMI (ODOO-335)
# only by a payer identity learned on 29/09. None matches her invoices; the
# 24/10 cross listed Aixa as «mayorista provisorio» and the 25/10 audit set
# aside another of its transfers. Nobody knows who Aixa is (ale, 29/10/2026):
# the 9 are set aside by hand, with no wholesale rule, and the learned CUIT is
# removed so the next imports leave Aixa unidentified. No balance changes.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CUIT = '27408994727'
uenv = env(user=env.ref('base.user_admin').id)
jaqui = uenv['res.partner'].sudo().browse(453)
assert jaqui.ref == 'ODOO-335'
cards = uenv['distri.mp.collection'].sudo().search([('kind', '=', 'income'), ('state', '=', 'draft'), ('payer_cuit', '=', CUIT)])
assert len(cards) == 9 and cards.mapped('partner_id') == jaqui, (len(cards), cards.mapped('partner_id.name'))
total = sum(cards.mapped('amount'))
before = jaqui._distri_account_balance()
identity = uenv['distri.payer.identity'].sudo().search([('kind', '=', 'cuit'), ('value', '=', CUIT)])
assert identity.partner_id == jaqui, identity.mapped('partner_id.name')
identity.unlink()
moved = cards._distri_set_aside(label='Aixa (CUIT 27-40899472-7): pagador sin identificar, no es Sandoval Jaqueline')
jaqui.message_post(body='Las %d transferencias de «Aixa» (CUIT 27-40899472-7, $ %s) que estaban propuestas para '
                        'este cliente quedaron apartadas: no coinciden con sus facturas y no se sabe quién es '
                        '(29/10/2026).' % (len(moved), '{:,.0f}'.format(total).replace(',', '.')))
uenv['distri.balance.review'].sudo()._distri_refresh(jaqui)
env.flush_all()
after = jaqui._distri_account_balance()
print('RESULT apartadas=%d importe=%.2f estados=%s saldo %.2f -> %.2f' % (
    len(moved), total, set(cards.mapped('state')), before, after))
if DO_COMMIT and len(moved) == 9 and abs(after - before) < 0.01:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
