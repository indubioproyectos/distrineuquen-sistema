# Receipts that arrived in the 29/10 WhatsApp export for customers reviewed on
# 29/10/2026: each one already matched to its Mercado Pago income on the board
# (same amount). Confirmed the board's way, now that the receipt is there.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
CARDS = {50423: ('ODOO-482', 284700.0), 27387: ('ODOO-180', 567700.0),
         26496: ('ODOO-722', 994650.0), 50404: ('ODOO-151', 450000.0)}
uenv = env(user=env.ref('base.user_admin').id)
Card = uenv['distri.mp.collection'].sudo()
ok = True
for cid, (ref, amount) in CARDS.items():
    card = Card.browse(cid)
    assert card.kind == 'income' and card.state == 'draft' and card.receipt_id, cid
    assert card.partner_id.ref == ref and abs(card.amount - amount) < 0.01, (cid, card.partner_id.ref, card.amount)
    partner = card.partner_id
    before = partner._distri_account_balance()
    card.with_context(distri_skip_confirm_notice=True).action_confirm()
    env.flush_all()
    after = partner._distri_account_balance()
    good = card.state == 'confirmed' and abs(before - after - amount) < 0.01
    ok = ok and good
    print('RESULT %s %s %s saldo %.2f -> %.2f' % (ref, partner.name[:40], card.payment_id.name, before, after))
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
