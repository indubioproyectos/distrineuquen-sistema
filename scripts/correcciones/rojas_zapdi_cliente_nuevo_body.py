# New customer, ale 04/11/2026: «ROJAS JULIAN RAUL - PELUQUERIA & BARBERIA ZAPDI (PELUQUERO SIMON)»,
# CUIT 23-39776455-9, created in Online as id 811 (cobranza/efectivo/crear_cliente_rojas_zapdi_online.py).
# The Online sync only brings customers with movements, so it is created here the same way the sync
# does (ref ODOO-811). Then the MP transfer of 28/08/2026 ($32.800, operation 176130319034,
# distri.mp.collection 25296, still a draft) moves from «X - AILIN ROJAS- KIOSCO EL REY» (ODOO-357),
# where it was assigned by hand, to the new customer.
# Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
P = uenv['res.partner'].sudo().with_context(active_test=False, no_vat_validation=True)
Col = uenv['distri.mp.collection'].sudo()

NAME = 'ROJAS JULIAN RAUL - PELUQUERIA & BARBERIA ZAPDI (PELUQUERO SIMON)'
assert not P.search(['|', ('ref', '=', 'ODOO-811'), ('vat', 'ilike', '23397764559')]), 'already in the lab'
src = P.search([('ref', '=', 'ODOO-357')])
assert len(src) == 1 and 'AILIN ROJAS' in src.name, src
col = Col.browse(25296)
assert col.exists() and col.partner_id == src and col.source_id == '176130319034', col
assert abs(col.amount - 32800) < 0.01 and col.state == 'draft' and not col.payment_id, col

b_src = src._distri_account_balance()
dst = P.create({'name': NAME, 'city': 'NEUQUEN', 'is_company': True, 'ref': 'ODOO-811',
                'customer_rank': 1, 'lang': 'es_AR'})
try:
    with env.cr.savepoint():
        dst.write({'vat': '23397764559'})
except Exception as exc:  # same as the sync: a rejected VAT does not stop the creation
    print('VAT rejected:', exc)
note = (col.proposal_note or '').strip()
col.write({'partner_id': dst.id,
           'proposal_note': ('%s · ' % note if note else '') +
           'pasado de ODOO-357 (Ailin Rojas) a ODOO-811 (cliente nuevo) el 04/11/2026: el comprobante es de '
           'Julian Raul Rojas, CUIT 23-39776455-9'})
env.flush_all()
print('RESULT', {'new_partner': dst.id, 'ref': dst.ref, 'vat': dst.vat, 'collection': col.id,
                 'collection_partner': col.partner_id.name, 'ailin_balance_before': b_src,
                 'ailin_balance_after': src._distri_account_balance(),
                 'new_balance': dst._distri_account_balance()})
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
