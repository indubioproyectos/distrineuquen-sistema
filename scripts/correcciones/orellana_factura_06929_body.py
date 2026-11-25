# Invoice 0001/2026/06929 (16/10/2026, $315.800) was billed in Online to
# ORELLANA MIRTHA - AUTOSERVICIO ALEX Y ADIS (365) but belongs to ORELLANA
# MICAELA - MERCADO MIKA (364): with it Micaela's 29/10 Mercado Pago payment
# of $405.200 (PMP/2026/00606) leaves her almost even instead of $314.200 in
# favor.
# 24/11/2026 11:40 it was first fixed with the AJC pair AJC/2026/00073 (credit
# note on Mirtha) and AJC/2026/00074 (debit note on Micaela). Gerencia asked
# the same day to undo that pair and move the invoice itself, so the
# statements a customer gets show no adjustment: the AJC pair (not reconciled,
# the last two AJC numbers) is deleted, and the history row moves to Micaela
# like irigoyen_factura_05998_body.py. The administrator moves it in Online.
# The concept marker «en el lab el DD/MM/AAAA» keeps the Online sync from
# undoing it (scripts/online/leer_online.py MANUAL_FIX).
# Rolls back unless DO_COMMIT=1.
import os
from markupsafe import Markup

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
AMOUNT = 315800.0
env = env(user=2)
P = env['res.partner'].sudo()
mirtha, micaela = P.browse(365), P.browse(364)
assert 'MIRTHA' in mirtha.name and 'MICAELA' in micaela.name
for p in (mirtha, micaela):
    env.cr.execute("SELECT id FROM res_partner WHERE id = %s FOR UPDATE", [p.id])
b_mi, b_mc = round(mirtha._distri_statement()['balance'], 2), round(micaela._distri_statement()['balance'], 2)
assert abs(b_mi - 810750) < 0.005 and abs(b_mc - 574490) < 0.005, (b_mi, b_mc)

# 1. undo the AJC pair
Move = env['account.move'].sudo()
ajc = Move.search([('name', 'in', ['AJC/2026/00073', 'AJC/2026/00074'])])
assert len(ajc) == 2 and set(ajc.partner_id.ids) == {364, 365}
assert not ajc.line_ids.matched_debit_ids and not ajc.line_ids.matched_credit_ids
last = Move.search([('journal_id', '=', ajc[0].journal_id.id), ('name', '!=', '/')], order='id desc', limit=2)
assert set(last.ids) == set(ajc.ids), last.mapped('name')
adj = env['distri.balance.adjustment'].sudo().search([('move_id', 'in', ajc.ids)])
assert len(adj) == 2
adj.unlink()
ajc.button_draft()
ajc.with_context(force_delete=True).unlink()

# 2. move the history row
row = env['distri.cuenta.movimiento'].sudo().search(
    [('partner_id', '=', mirtha.id), ('comprobante', '=', '0001/2026/06929')])
assert len(row) == 1 and row.tipo == 'factura' and abs(row.debe - AMOUNT) < 0.01
row.write({'partner_id': micaela.id,
           # Mirtha's red-review mark does not travel; the FIFO computes Micaela's
           'distri_owed': False, 'distri_owed_amount': 0.0, 'distri_owed_note': False,
           'concepto': '%s · corregida en el lab el 24/11/2026' % (row.concepto or '')})
note = ('Factura 0001/2026/06929 (16/10, $ 315.800) pasada de ORELLANA MIRTHA a ORELLANA MICAELA en el lab '
        '(24/11/2026, autorizó Gerencia). Reemplaza el ajuste AJC/2026/00073 y 00074 del mismo día, que se borró. '
        'Falta cambiarla en Online.')
for p in (mirtha, micaela):
    p.message_post(body=Markup('<b>Corrección de cuenta</b>: ') + note)
env.flush_all()
env['distri.balance.review'].sudo()._distri_refresh(mirtha | micaela)
env.flush_all()
a_mi, a_mc = round(mirtha._distri_statement()['balance'], 2), round(micaela._distri_statement()['balance'], 2)
for p in (mirtha, micaela):
    rows = p._distri_statement_rows()
    print('ROJO %s %s' % (p.name, [(r.get('number'), r.get('owed')) for r in rows if r.get('owed')]))
print('RESULT mirtha %.2f -> %.2f | micaela %.2f -> %.2f | ajc quedan %s' % (
    b_mi, a_mi, b_mc, a_mc, Move.search_count([('id', 'in', ajc.ids)])))
ok = abs(a_mi - b_mi) < 0.005 and abs(a_mc - b_mc) < 0.005
if DO_COMMIT and ok:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED', 'ok' if ok else 'BALANCES DIFFER')
