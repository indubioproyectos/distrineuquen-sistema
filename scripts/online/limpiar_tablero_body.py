# Empties the collections board of a lab database before a live catch-up
# (odoo shell body). Removes the Mercado Pago reports, their incomes and
# outgoing transfers, the daily closes and the WhatsApp messages with their
# files. Never touches confirmed payments: it stops if any card has one.
# Keeps the configuration: customers' CUIT/phones and the wholesale payers.
# Commits only with DO_COMMIT=1.
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
Collection = env['distri.mp.collection'].sudo()
paid = Collection.search_count([('payment_id', '!=', False)])
if paid:
    raise SystemExit('Hay %s cobros con pago registrado: no se limpia nada.' % paid)
Message = env['distri.whatsapp.message'].sudo()
if env['sale.order'].sudo().search_count(['|', '|', ('distri_agil_message_id', '!=', False),
                                          ('distri_wa_message_id', '!=', False),
                                          ('distri_wa_proposal_id', '!=', False)]):
    raise SystemExit('Hay pedidos unidos a mensajes de WhatsApp: no se limpia nada.')
counts = {
    'cobros': Collection.search_count([]),
    'reportes': env['distri.mp.report'].sudo().search_count([]),
    'egresos': env['distri.mp.outgoing'].sudo().search_count([]),
    'cierres': env['distri.mp.day'].sudo().search_count([]),
    'mensajes': Message.search_count([]),
}
Collection.search([]).unlink()
reports = env['distri.mp.report'].sudo().search([])
reports.attachment_id.unlink()
reports.unlink()
env['distri.mp.outgoing'].sudo().search([]).unlink()
env['distri.mp.day'].sudo().search([]).unlink()
messages = Message.search([])
env['ir.attachment'].sudo().search([('res_model', '=', 'distri.whatsapp.message'),
                                    ('res_id', 'in', messages.ids)]).unlink()
messages.unlink()
env['distri.whatsapp.chat.import'].sudo().search([]).unlink()
print('LIMPIEZA', counts)
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
