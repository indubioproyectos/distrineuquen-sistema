# ALBARRÁN GUILLERMO - MERCADO LOS GUILLES (ODOO-47): MP payments already in
# Online, reviewed with ale on 29/10/2026. Each Italo caption «ALBARRAN
# GUILLERMO» in the group matches one Online payment and one MP card:
#   130383719083 (23/10 01:04) = BNK1/2025/01558 (22/10): confirmed by mistake
#   131190620853 (30/10 00:17) = BNK1/2025/01637
#   132103026968 (01/11 13:23) = BNK1/2025/01667
#   131577613113 (02/11 00:08) = BNK1/2025/01672
# The confirmed one is undone (its PMP payment cancelled) and all four are
# discarded as already in Online. The JUAN RAMON TAPIA cards stay pending.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
partner = uenv['res.partner'].sudo().browse(16)
assert partner.ref == 'ODOO-47', partner.ref
C = uenv['distri.mp.collection'].sudo()
ONLINE = {
    '130383719083': 'BNK1/2025/01558 (22/10/2025)',
    '131190620853': 'BNK1/2025/01637 (30/10/2025)',
    '132103026968': 'BNK1/2025/01667 (01/11/2025)',
    '131577613113': 'BNK1/2025/01672 (02/11/2025)',
}
before = partner._distri_account_balance()
done = []
for source, online in ONLINE.items():
    card = C.search([('source_id', '=', source), ('kind', '=', 'income')])
    assert len(card) == 1, source
    assert card.partner_id in (partner, partner.browse()), (source, card.partner_id.name)
    if card.state == 'confirmed':
        card.action_undo()
    assert card.state == 'draft', (source, card.state)
    note = 'Ya en Online: %s; leyenda de Italo en el grupo (revisado 29/10/2026)' % online
    card.write({'partner_id': partner.id, 'state': 'ignored', 'ignore_reason': 'other',
                'ignore_note': note})
    card.message_post(body='Descartado: %s.' % note)
    done.append(source)
uenv['distri.balance.review'].sudo()._distri_refresh(partner)
env.flush_all()
after = partner._distri_account_balance()
print('RESULT tarjetas=%s saldo_antes=%.2f saldo_despues=%.2f' % (done, before, after))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
