# Cash-sheet cross (planillas de efectivo 2026), «dudosos», ale 31/10/2026.
# Each fix was found by one agent and confirmed by a second, blind one
# (sheet REND = column sum, Mercado Pago payer «kioscolachichu» = CUIT of
# RODRIGUEZ MICAELA). The lab's copy of the history is corrected (Online is
# fixed by the administrator); the 213.600 cash of 28/07 is loaded apart with
# the sheet loader. Rolls back unless DO_COMMIT=1.
#   RODRIGUEZ MICAELA (ODOO-369):
#     PCSH1/2026/00798 26/05 $134.100 -> $138.110 (sheet 26/05, invoice 03030)
#     PBNK1/2026/02156 06/05 $145.950 archived: repeats PBNK1/2026/01788 (one transfer, op. 162877905912)
#     PBNK1/2026/01307 09/05 $91.000 moved from ORELLANA MICAELA (ODOO-181): op. 158551577582, payer kioscolachichu
#     PBNK1/2026/03322 29/08 $151.200 -> $152.100 (op. 175261075355)
#     MP card 22193 (17/04 $18.000, op. 154559638833) approved: not loaded anywhere
#   MEDELE (ODOO-78): PCSH1/2026/00258 13/03 $216.600 -> $200.000 (Sevila's $216.600 is PCSH1/2026/00582)
#   PCSH1/2026/01518 14/08 $150.000 moved from VEGA VALERIA (ODOO-361) to PEREYRA ISAIAS (JUMBO) (ODOO-140)
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
P = uenv['res.partner'].sudo()
Mov = uenv['distri.cuenta.movimiento'].sudo().with_context(active_test=False)
Mirror = uenv['distri.online.payment'].sudo()
MARK = 'en el lab el 31/10/2026'


def partner(ref, label):
    p = P.search([('ref', '=', ref)])
    assert len(p) == 1 and label in p.name, ref
    return p


def row(name, owner, amount):
    r = Mov.search([('comprobante', '=', name), ('tipo', '=', 'pago')])
    assert len(r) == 1 and r.partner_id == owner and r.active and abs(r.haber - amount) < 0.01, name
    return r


micaela = partner('ODOO-369', 'RODRIGUEZ MICAELA')
orellana = partner('ODOO-181', 'ORELLANA MICAELA')
medele = partner('ODOO-78', 'MEDELE')
vega = partner('ODOO-361', 'VEGA VALERIA')
jumbo = partner('ODOO-140', 'JUMBO')
people = micaela | orellana | medele | vega | jumbo
before = {p.id: p._distri_account_balance() for p in people}


def amount_fix(name, owner, old, new, why):
    r = row(name, owner, old)
    r.write({'haber': new, 'concepto': '%s · importe corregido %s: en Online figura $%s; %s'
             % (r.concepto or '', MARK, '{:,.0f}'.format(old).replace(',', '.'), why)})
    m = Mirror.search([('name', '=', name)])
    if len(m) == 1:
        m.write({'amount': new})


def move(name, src, dst, amount, why):
    r = row(name, src, amount)
    r.write({'partner_id': dst.id, 'concepto': '%s · pasado de %s a %s %s (%s)'
             % (r.concepto or '', src.ref, dst.ref, MARK, why)})
    m = Mirror.search([('name', '=', name)])
    if len(m) == 1:
        m.write({'partner_id': dst.id})


amount_fix('PCSH1/2026/00798', micaela, 134100.0, 138110.0, 'planilla de Simón del 26/05, factura 03030')
amount_fix('PBNK1/2026/03322', micaela, 151200.0, 152100.0, 'Mercado Pago op. 175261075355')
dup = row('PBNK1/2026/02156', micaela, 145950.0)
keep = row('PBNK1/2026/01788', micaela, 145950.0)
dup.write({'active': False, 'concepto': '%s · repetido de PBNK1/2026/01788 (una sola transferencia, op. '
           '162877905912); archivado %s, falta anularlo en Online' % (dup.concepto or '', MARK)})
move('PBNK1/2026/01307', orellana, micaela, 91000.0, 'Mercado Pago op. 158551577582, pagador kioscolachichu')
amount_fix('PCSH1/2026/00258', medele, 216600.0, 200000.0,
           'planilla de Simón (Centenario) del 13/03; los $216.600 son de Sevila (PCSH1/2026/00582)')
move('PCSH1/2026/01518', vega, jumbo, 150000.0, 'planilla de Jorge del 14/08: «ISAIAS JUMBO 150000»')

card = uenv['distri.mp.collection'].sudo().browse(22193)
assert card.state == 'draft' and card.partner_id == micaela and abs(card.amount - 18000) < 0.01
if card.receipt_candidate and not card.receipt_id:
    uenv['distri.mp.vouch'].sudo().create({'collection_id': card.id, 'note':
        'Lo confirmó ale el 31/10/2026: transferencia de kioscolachichu (Rodríguez Micaela) del 17/04, '
        'no cargada en Online; cruce de las planillas de efectivo'}).action_apply()
else:
    card.with_context(distri_vouched_ids=card.ids).action_confirm()
    card.message_post(body='Aprobada el 31/10/2026 (ale): transferencia de kioscolachichu (Rodríguez Micaela) '
                           'del 17/04, no cargada en Online; cruce de las planillas de efectivo.')
assert card.state != 'draft' and card.payment_id, card.state

notes = {micaela: 'Corregido al cruzar las planillas de efectivo (31/10/2026): 00798 $134.100 → $138.110, '
                  '03322 $151.200 → $152.100, 02156 $145.950 archivado (repite 01788), 01307 $91.000 traído '
                  'de Orellana Micaela, transferencia de $18.000 del 17/04 aprobada.',
         orellana: 'PBNK1/2026/01307 ($91.000, 09/05) pasado a RODRIGUEZ MICAELA: la transferencia es de '
                   'kioscolachichu (31/10/2026).',
         medele: 'PCSH1/2026/00258 (13/03) corregido de $216.600 a $200.000 según la planilla (31/10/2026).',
         vega: 'PCSH1/2026/01518 ($150.000, 14/08) pasado a PEREYRA ISAIAS (JUMBO): en la planilla de Jorge es '
               'de Jumbo (31/10/2026).',
         jumbo: 'Se trajo PCSH1/2026/01518 ($150.000, 14/08) desde VEGA VALERIA: en la planilla de Jorge es de '
                'Jumbo (31/10/2026).'}
for p, text in notes.items():
    p.message_post(body=text)
uenv['distri.balance.review'].sudo()._distri_refresh(people)
env.flush_all()
after = {p.id: p._distri_account_balance() for p in people}
expected = {micaela.id: -4010 - 900 + 145950 - 91000 - 18000, orellana.id: 91000, medele.id: 16600,
            vega.id: 150000, jumbo.id: -150000}
ok = True
for p in people:
    moved = after[p.id] - before[p.id]
    ok = ok and abs(moved - expected[p.id]) < 0.01
    print('RESULT', p.name, 'saldo %.2f -> %.2f (%+.2f, esperado %+.2f)' % (before[p.id], after[p.id], moved,
                                                                           expected[p.id]))
print('RESULT tarjeta', card.name, card.state, card.payment_id.name)
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
