# Simón could not settle route 142 (Simón · 20/11 · Viaje 2): his visit to
# CASTILLO INGRID (S00299, stop 304) was rejected (distri.sync.rejection 4)
# because he entered a $492.000 transfer and the invoice 0001/2026/08145 was
# already paid by Mercado Pago on 20/11 (PMP/2026/01225). The app does not
# let him set the amount to 0. Ale (24/11/2026): resend that visit as Simón,
# delivered, without transfer, as a resend of the rejected one; the normal
# sync code records it and closes the rejection. Rolls back unless DO_COMMIT=1.
import os, uuid
DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
simon = env['res.users'].browse(7)
assert simon.login == 'simon'
rej = env['distri.sync.rejection'].sudo().browse(4)
assert rej.state == 'open' and rej.stop_id.id == 304 and rej.route_id.id == 142
stop = rej.stop_id
assert stop.order_id.name == 'S00299' and stop.state == 'pending'
inv = stop.order_id.invoice_ids.filtered(lambda m: m.state == 'posted')
assert inv and all(m.payment_state == 'paid' for m in inv)
payload = {"visit_uuid": str(uuid.uuid4()), "stop_id": 304, "outcome": "delivered", "reason": "",
           "returns_answer": "none", "returns": [], "code": "", "without_code": True,
           "code_attempts": 0, "code_already_ok": False, "cash": 0, "transfer": "no",
           "transfer_amount": 0, "receipt_answer": None, "no_payment": False,
           "comment": "Ya pagado por Mercado Pago el 20/11 (PMP/2026/01225). Reenviado por Sistemas 24/11.",
           "gps": ""}
event = {"uuid": str(uuid.uuid4()), "type": "visit", "route_id": 142,
         "captured_at": "2026-11-24T16:47:18.809Z", "resends": rej.uuid, "payload": payload}
res = env['distri.sync.event'].with_user(simon).sudo().process_batch(simon, [event])
env.flush_all(); env.invalidate_all()
print("RESULT", res)
print("RESULT stop", stop.state, stop.delivery_state, "| rechazo", rej.state, "| abiertos", env['distri.sync.rejection'].sudo().search_count([('route_id','=',142),('state','=','open')]))
if DO_COMMIT and rej.state != 'open':
    env.cr.commit(); print("COMMITTED")
else:
    env.cr.rollback(); print("NOT COMMITTED")
