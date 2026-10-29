# odoo shell body of ordenar-minuto.sh. One import per owner is expected:
# the manager's export (chat_gerente.zip) and the own one (... - Alejandro.zip).
import json
import os

Import = env['distri.whatsapp.chat.import'].sudo()
Message = env['distri.whatsapp.message'].sudo()
ok = True
for owner, path in (('manager', '/chat/manager.zip'), ('own', '/chat/own.zip')):
    records = Import.search([('distri_export_owner', '=', owner)])
    if len(records) != 1:
        print('ABORT', owner, 'imports:', len(records))
        ok = False
        break
    print('RESULT', owner, records.id, json.dumps(records._distri_backfill_minute_pos(path)))
print('RESULT sin posición:', Message.search_count([
    ('distri_chat_import_id', '!=', False), ('distri_chat_minute_pos', '=', 0)]))
if ok and os.environ.get('DO_COMMIT') == '1':
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
