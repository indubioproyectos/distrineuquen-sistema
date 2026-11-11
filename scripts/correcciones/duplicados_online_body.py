# Online payments loaded twice for one Mercado Pago transfer (search of
# 30/10/2026: same customer, same day, same amount, two Bank payments and a
# single MP transfer). ale: leave them corrected in the lab, he fixes Online.
# The lab's copy of the repeated history row is archived (like Camacho's
# PBNK1/2026/00932). Skipped: Josse (Zapala, balance agreed with the manager)
# and Oviedo (to see with the administrator). A customer with an AJC
# adjustment after the payment is skipped too. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
DUPS = [  # online ref, repeated payment, kept payment, amount
    ('ODOO-457', 'PBNK1/2026/01018', 'PBNK1/2026/00920', 60000.0),   # Gonzalez Angeles
    ('ODOO-0', 'PBNK1/2026/00992', 'PBNK1/2026/00881', 81950.0),     # Oscar Martin (ref looked up)
    ('ODOO-0', 'PBNK1/2026/01617', 'PBNK1/2026/01601', 121440.0),    # Medele
    ('ODOO-0', 'PBNK1/2026/02003', 'PBNK1/2026/02000', 50150.0),     # Leiva
    ('ODOO-0', 'PBNK1/2026/02246', 'PBNK1/2026/02115', 100700.0),    # Espindola
    ('ODOO-0', 'PBNK1/2026/02814', 'PBNK1/2026/02810', 90500.0),     # Lopez Ramon
    ('ODOO-0', 'PBNK1/2026/02949', 'PBNK1/2026/02947', 182700.0),    # Lucia Qiu
    ('ODOO-0', 'PBNK1/2026/04045', 'PBNK1/2026/04024', 136500.0),    # Despensa Mateo
    ('ODOO-463', 'PBNK1/2026/04075', 'PBNK1/2026/04071', 146900.0),  # Maldonado Celina
    ('ODOO-0', 'PBNK1/2026/04141', 'PBNK1/2026/04140', 50000.0),     # Sepulveda
]
Mov = env['distri.cuenta.movimiento'].sudo()
Adj = env['distri.balance.adjustment'].sudo()
done = []
for _ref, dup, keep, amount in DUPS:
    row = Mov.search([('comprobante', '=', dup), ('tipo', '=', 'pago')])
    other = Mov.search([('comprobante', '=', keep), ('tipo', '=', 'pago')])
    if len(row) != 1 or len(other) != 1 or row.partner_id != other.partner_id \
            or abs(row.haber - amount) > 0.5 or abs(other.haber - amount) > 0.5:
        print('SKIP', dup, 'filas', len(row), len(other)); continue
    partner = row.partner_id.commercial_partner_id
    adjs = Adj.search([('partner_id', '=', partner.id), ('date', '>=', row.fecha)])
    if adjs:
        print('SKIP', dup, partner.name, 'ajuste posterior', adjs.mapped('name')); continue
    before = partner._distri_account_balance()
    row.write({'active': False, 'concepto': '%s · repetido de %s (una sola transferencia); archivado en el lab el '
                                           '30/10/2026, falta anularlo en Online' % (row.concepto or '', keep)})
    partner.message_post(body='%s ($ %s) repite %s: una sola transferencia de Mercado Pago. Archivado en la historia '
                              'del lab (30/10/2026); falta anularlo en Online.' % (dup, amount, keep))
    env['distri.balance.review'].sudo()._distri_refresh(partner)
    env.flush_all()
    after = partner._distri_account_balance()
    ok = abs(after - before - amount) < 0.01
    done.append(ok)
    print('RESULT', dup, partner.ref, partner.name[:35], 'saldo %.2f -> %.2f' % (before, after), 'ok' if ok else 'NO CIERRA')
if DO_COMMIT and done and all(done):
    env.cr.commit()
    print('COMMITTED', len(done))
else:
    env.cr.rollback()
    print('NOT COMMITTED', len(done))
