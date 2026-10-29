# Undo of cargar-efectivo.sh (odoo shell body, run by deshacer-efectivo.sh):
# the payments listed in /in/ids.txt (pagos-creados-<stamp>.txt of the load)
# are moved to draft and cancelled, as the 24/10 corrections do
# (correcciones_body.py: action_draft + action_cancel); the key gets the
# suffix «_deshecho» so the load can run again. Only payments of «Efectivo»
# (CSH1) with a sheet key distri_cobro_ef_… and a memo «Efectivo · cobró …
# · planilla …» are touched (ABORT otherwise). Each customer balance must go
# back up by exactly the cancelled amount. Rolls back unless DO_COMMIT=1.
import json
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
IN = os.environ.get('IN_DIR', '/in')
Payment = env['account.payment'].sudo()
Partner = env['res.partner'].sudo()


def abort(message):
    env.cr.rollback()
    print('ABORT', message)
    raise SystemExit(1)


with open(os.path.join(IN, 'ids.txt')) as handle:
    ids = [int(line) for line in handle if line.strip()]
payments = Payment.browse(ids).exists()
if len(payments) != len(ids):
    abort('faltan pagos: %s de %s' % (len(payments), len(ids)))
for pay in payments:
    if pay.journal_id.code != 'CSH1' or not (pay.payment_reference or '').startswith('distri_cobro_ef_') \
            or not (pay.memo or '').startswith('Efectivo · cobró ') or pay.payment_type != 'inbound':
        abort('el pago %s (%s) no es de esta carga' % (pay.id, pay.name))
active = payments.filtered(lambda p: p.state != 'canceled')
partners = active.mapped('partner_id.commercial_partner_id')
before = {p.id: p._distri_account_balance() for p in partners}
by_partner = {}
for pay in active:
    by_partner[pay.partner_id.commercial_partner_id.id] = by_partner.get(pay.partner_id.commercial_partner_id.id, 0) + pay.amount
    pay.action_draft()
    pay.action_cancel()
    pay.payment_reference = pay.payment_reference + '_deshecho'
    print('LOG', json.dumps({'accion': 'anulado', 'pago': pay.id, 'nombre': pay.name, 'importe': pay.amount}, ensure_ascii=False))
env.flush_all()
for pid, amount in by_partner.items():
    moved = Partner.browse(pid)._distri_account_balance() - before[pid]
    if abs(moved - amount) > 0.01:
        abort('el saldo del cliente %s subió %s y no %s' % (pid, moved, amount))
print('RESULT', json.dumps({'pagos': len(ids), 'anulados': len(active), 'ya_anulados': len(payments) - len(active),
                            'importe': round(sum(active.mapped('amount')), 2)}))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
