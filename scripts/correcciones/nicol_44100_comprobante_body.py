# Receipt without money, ale 04/11/2026: the Ualá receipt of NICOL (distri.whatsapp.message 14996,
# sent by Jorge on 02/11/2026; $44.100 at 16:05, payer Nain Maximiliano Torres, CUIL 20-43946801-8)
# is the MP income 186341715217 of report 118 (distri.mp.collection 50615: $44.100 at 16:05:19 AR,
# bank to CVU, no payer data, draft with no customer). Checked by an independent agent: same amount,
# same minute, no other receipt or income of that amount between 25/10 and 04/11.
# Fills the receipt's amount and payer, ties it to the income with the module's own _link_receipt
# (closes its «Sin transferencia» card) and proposes NICOL (ODOO ref of lab 356). The income stays a
# draft: the administrator confirms it on the board. Rolls back unless DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
uenv = env(user=env.ref('base.user_admin').id)
Msg = uenv['distri.whatsapp.message'].sudo()
Col = uenv['distri.mp.collection'].sudo()
P = uenv['res.partner'].sudo()

receipt, income, nicol = Msg.browse(14996), Col.browse(50615), P.browse(356)
assert receipt.exists() and receipt.distri_receipt_state == 'no_money' and receipt.distri_partner_id == nicol, receipt
assert nicol.name == 'NICOL', nicol.name
assert income.exists() and income.source_id == '186341715217' and abs(income.amount - 44100) < 0.01, income
assert income.state == 'draft' and not income.receipt_id and not income.partner_id and not income.payment_id, income
assert not Msg.search([('distri_collection_id', '=', income.id)]), 'income already tied'

receipt.write({'distri_amount': 44100.0, 'distri_payer_name': 'Nain maximiliano Torres',
               'distri_payer_cuit': '20439468018'})
income._link_receipt(receipt)
income.write({'partner_id': nicol.id, 'proposal': 'manual',
              'proposal_note': 'Comprobante Ualá de NICOL del 02/11 16:05 (Jorge), unido a mano el 04/11/2026: '
                               'mismo monto y minuto'})
env.flush_all()
cards = Col.search([('receipt_id', '=', receipt.id)])
print('RESULT', {'receipt_state': receipt.distri_receipt_state, 'receipt_collection': receipt.distri_collection_id.id,
                 'income_partner': income.partner_id.name, 'income_state': income.state, 'income_column': income.column,
                 'cards': [(c.id, c.kind, c.state) for c in cards]})
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
