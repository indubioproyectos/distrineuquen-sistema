# Captions lost by the chat import before 19.0.1.60.0: a seller writing the
# same customer under two photos of the same minute sent two identical text
# lines, and the import kept one (chat_text_key had no occurrence number).
# Reads the exports again (/in/<file>, one per import listed in IMPORTS) and
# creates only the text messages whose new key (chat_text_key with nth > 1)
# is missing, attached to the import that brought that export. Creates
# nothing else and never touches photos. Checks that no card or receipt
# changes state, customer or amount. Rolls back unless DO_COMMIT=1.
import json
import os
from datetime import timezone

from odoo.addons.distri_pedido.models.puesta_al_dia import (
    AR_TZ, _Source, _sender_key, chat_text_key, minute_positions, parse_chat)

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IMPORTS = json.loads(os.environ['IMPORTS'])  # {"<import id>": "<file in /in>"}
Message = env['distri.whatsapp.message'].sudo()
Card = env['distri.mp.collection'].sudo()


def utc(local):
    return local.replace(tzinfo=AR_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def snapshot():
    cards = Card.search_read([], ['state', 'partner_id', 'amount', 'receipt_id'])
    receipts = Message.search_read([('message_type', '!=', 'text')],
                                   ['distri_receipt_state', 'distri_partner_id'])
    return ({c['id']: (c['state'], c['partner_id'], c['amount'], c['receipt_id']) for c in cards},
            {m['id']: (m['distri_receipt_state'], m['distri_partner_id']) for m in receipts})


before = snapshot()
seen = set(Message.search([('wa_message_id', '=like', 'chat:t:%')]).mapped('wa_message_id'))
created = []
for import_id, name in IMPORTS.items():
    record = env['distri.whatsapp.chat.import'].sudo().browse(int(import_id))
    assert record.exists(), import_id
    rows = parse_chat(_Source(path='/in/' + name).chat_text())
    repeats = {}
    for (when, sender, body, att), pos in zip(rows, minute_positions(rows)):
        if att or not body or body.startswith('<') and body.endswith('>'):
            continue
        if not (record.date_from <= when.date() <= record.date_to):
            continue
        repeats[(when, sender, body)] = nth = repeats.get((when, sender, body), 0) + 1
        key = chat_text_key(when, sender, body, nth)
        if nth == 1 or key in seen:
            continue
        seen.add(key)
        msg = Message.create({
            'wa_message_id': key, 'direction': 'in', 'message_type': 'text',
            'wa_id': _sender_key(sender), 'profile_name': sender[:64], 'body': body[:4000],
            'timestamp': utc(when), 'distri_chat_import_id': record.id,
            'distri_chat_sender': sender[:64], 'distri_chat_minute_pos': pos})
        created.append(msg)
        print('LOG %s' % json.dumps({'import': record.id, 'id': msg.id, 'when': when.isoformat(),
                                     'sender': sender, 'body': body[:80], 'nth': nth,
                                     'pos': pos}, ensure_ascii=False))
env.flush_all()
after = snapshot()
changed_cards = [k for k in before[0] if before[0][k] != after[0].get(k)]
changed_receipts = [k for k in before[1] if before[1][k] != after[1].get(k)]
print('RESULT %s' % json.dumps({'creados': len(created), 'tarjetas_cambiadas': changed_cards,
                                'comprobantes_cambiados': changed_receipts}))
if DO_COMMIT and not changed_cards and not changed_receipts:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
