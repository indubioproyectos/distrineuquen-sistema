# Draft MP cards whose payment is already in Online as a BNK1 bank entry (2025
# to early 2026) or an unlinked PBNK1. The Online payments mirror has no BNK1,
# so the board never saw them; the 25/10 old-period pass missed these 20.
# Each pair: same customer, same amount, Online date 1 day before to 2 days
# after, the Online entry not claimed by any other card. Reviewed with ale on
# 29/10/2026. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
PAIRS = [  # MP operation, lab partner id, Online entry, Online date
    ('111508003768', 493, 'BNK1/2025/00219', '14/05/2025'),
    ('111646347930', 46, 'BNK1/2025/00470', '14/05/2025'),
    ('114308498031', 493, 'BNK1/2025/00448', '12/06/2025'),
    ('114799500428', 493, 'BNK1/2025/00410', '12/06/2025'),
    ('114735702585', 493, 'BNK1/2025/00407', '17/06/2025'),
    ('122003982934', 438, 'BNK1/2025/00795', '12/08/2025'),
    ('127057663591', 163, 'BNK1/2025/01273', '26/09/2025'),
    ('131798071321', 459, 'BNK1/2025/01695', '03/11/2025'),
    ('133874309411', 459, 'BNK1/2025/01874', '19/11/2025'),
    ('134830619403', 239, 'BNK1/2025/01995', '28/11/2025'),
    ('137390281595', 184, 'BNK1/2025/02191', '15/12/2025'),
    ('138427117498', 184, 'BNK1/2025/02213', '17/12/2025'),
    ('138947004570', 280, 'BNK1/2025/02280', '21/12/2025'),
    ('144932601106', 459, 'BNK1/2026/00381', '05/02/2026'),
    ('145297747042', 280, 'BNK1/2026/00417', '07/02/2026'),
    ('144883074841', 292, 'BNK1/2026/00446', '09/02/2026'),
    ('145102875431', 292, 'BNK1/2026/00467', '11/02/2026'),
    ('145685009835', 280, 'BNK1/2026/00514', '14/02/2026'),
    ('146745322872', 292, 'BNK1/2026/00540', '17/02/2026'),
    ('172381701658', 79, 'PBNK1/2026/02823', '07/08/2026'),
]
C = uenv['distri.mp.collection'].sudo()
Mov = uenv['distri.cuenta.movimiento'].sudo()
partners = uenv['res.partner'].sudo().browse(sorted({p for _, p, _, _ in PAIRS}))
before = {p.id: p._distri_account_balance() for p in partners}
assert len({o for _, _, o, _ in PAIRS}) == len(PAIRS)
for source, pid, online, day in PAIRS:
    card = C.search([('source_id', '=', source), ('kind', '=', 'income')])
    assert len(card) == 1 and card.state == 'draft' and card.partner_id.id == pid, (source, card.state, card.partner_id.id)
    row = Mov.search([('partner_id', '=', pid), ('comprobante', '=', online)])
    assert len(row) == 1 and abs(row.haber - card.amount) < 0.01, (source, online, row.mapped('haber'))
    # «: BNK1/…» never matches inside «PBNK1/…»
    assert not C.search_count([('ignore_note', 'ilike', ': ' + online)]), online
    note = 'Ya en Online: %s (%s); revisado 29/10/2026' % (online, day)
    card.write({'state': 'ignored', 'ignore_reason': 'other', 'ignore_note': note})
    card.message_post(body='Descartado: %s.' % note)
uenv['distri.balance.review'].sudo()._distri_refresh(partners)
env.flush_all()
changed = {p.name: (before[p.id], p._distri_account_balance()) for p in partners
           if abs(before[p.id] - p._distri_account_balance()) > 0.01}
print('RESULT tarjetas=%d clientes=%d saldos_cambiados=%s' % (len(PAIRS), len(partners), changed))
if DO_COMMIT and not changed:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
