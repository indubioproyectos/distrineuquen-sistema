# Applies the read-only download of Odoo Online to a lab database (odoo shell
# body, run by scripts/online/sincronizar-online.sh). No real data lives here:
# PAYLOAD_PATH points at insumos/online/<date>/payload.json, mounted read only.
#   1. new customers of Online (ref ODOO-<Online id>);
#   2. the history of each customer (distri.cuenta.movimiento: one row per
#      posted receivable line of Online, origen_id = its id), new, changed and
#      deleted rows, archived ones included;
#   3. the last invoice number of Online (the lab continues that series);
#   4. the mirror of Online's customer payments (distri.online.payment) and its
#      links to the Mercado Pago incomes of the lab («Ya cargado en Online»,
#      exported payments found by memo, history rows of lab payments archived);
#   5. check: each customer's history adds up to its Online balance.
# Commits only with DO_COMMIT=1 and when every balance matches.
import json
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
PAYLOAD = json.load(open(os.environ['PAYLOAD_PATH'], encoding='utf-8'))
Partner = env['res.partner'].sudo().with_context(active_test=False, no_vat_validation=True)
Mov = env['distri.cuenta.movimiento'].sudo().with_context(active_test=False)


def partner_map():
    return {int(p.ref[5:]): p.id for p in Partner.search([('ref', '=like', 'ODOO-%')])
            if p.ref[5:].isdigit()}


result = {'read_at': PAYLOAD.get('read_at'), 'partners_created': 0, 'vat_rejected': 0}
for data in sorted(PAYLOAD['newp'], key=lambda d: bool(d['parent'])):
    pmap = partner_map()
    vals = {k: data[k] for k in ('name', 'phone', 'email', 'street', 'city', 'is_company') if data[k]}
    vals.update(ref='ODOO-%s' % data['oid'], customer_rank=1, lang='es_AR')
    if data['parent'] and data['parent'] in pmap:
        vals['parent_id'] = pmap[data['parent']]
    partner = Partner.create(vals)
    if data['vat']:
        try:
            with env.cr.savepoint():
                partner.write({'vat': data['vat']})
        except Exception:
            result['vat_rejected'] += 1
    result['partners_created'] += 1

pmap = partner_map()
missing_partner = 0
new_vals = []
for r in PAYLOAD['new']:
    if r['p'] not in pmap:
        missing_partner += 1
        continue
    new_vals.append({'origen_id': r['o'], 'partner_id': pmap[r['p']], 'fecha': r['f'], 'comprobante': r['c'],
                     'concepto': r['k'], 'tipo': r['t'], 'debe': r['d'], 'haber': r['h']})
Mov.create(new_vals)
for r in PAYLOAD['upd']:
    Mov.search([('origen_id', '=', r['o'])]).write(
        {'fecha': r['f'], 'comprobante': r['c'], 'concepto': r['k'], 'debe': r['d'], 'haber': r['h']})
Mov.search([('origen_id', 'in', PAYLOAD['del'])]).unlink()

ICP = env['ir.config_parameter'].sudo()
if PAYLOAD.get('last_inv'):
    ICP.set_param('distri_pedido.factura_ultimo_numero_online', PAYLOAD['last_inv'])

result['payments'] = env['distri.online.payment'].sudo()._distri_load(PAYLOAD['payments'])

env.flush_all()
env.cr.execute("""select p.ref, round(sum(m.debe - m.haber), 2) from distri_cuenta_movimiento m
                  join res_partner p on p.id = m.partner_id group by p.ref""")
lab = {int(ref[5:]): float(v) for ref, v in env.cr.fetchall() if ref and ref.startswith('ODOO-') and ref[5:].isdigit()}
exp = {int(k): v for k, v in PAYLOAD['exp'].items()}
diff = {k: (exp.get(k, 0.0), lab.get(k, 0.0)) for k in set(exp) | set(lab)
        if abs(exp.get(k, 0.0) - lab.get(k, 0.0)) > 0.005}
result.update(created_rows=len(new_vals), updated_rows=len(PAYLOAD['upd']), deleted_rows=len(PAYLOAD['del']),
              rows_without_partner=missing_partner, customers_compared=len(exp),
              balance_mismatches=len(diff), mismatch_ids=sorted(diff)[:20],
              receivable_total_online=round(sum(exp.values()), 2), receivable_total_lab=round(sum(lab.values()), 2))
print("RESULT", json.dumps(result, ensure_ascii=False, default=str))
if DO_COMMIT and not diff:
    env.cr.commit()
    print("COMMITTED")
else:
    env.cr.rollback()
    print("NOT COMMITTED" + (" (balances differ)" if diff else " (DO_COMMIT is not 1)"))
