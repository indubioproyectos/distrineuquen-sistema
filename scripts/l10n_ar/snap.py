# Read-only snapshot of every customer balance, the accounting moves and
# payments, and the imported history. Run with SNAP_TAG=<name>; writes
# /scripts/out/snap_<name>.json. Never commits.
import json, os
tag = os.environ.get('SNAP_TAG') or 'before'
P = env['res.partner'].sudo()
Mov = env['distri.cuenta.movimiento'].sudo()
ids = set(Mov.search([]).mapped('partner_id.commercial_partner_id').ids)
ids |= set(env['account.move.line'].sudo().search([('account_id.account_type', '=', 'asset_receivable')]).mapped('partner_id.commercial_partner_id').ids)
out = {}
for p in P.browse(sorted(ids)):
    out[p.id] = round(p._distri_account_balance(), 2)
moves = []
for m in env['account.move'].sudo().search([], order='id'):
    moves.append(dict(id=m.id, name=m.name, type=m.move_type, partner=m.partner_id.id, date=str(m.date),
        total=m.amount_total, tax=m.amount_tax, residual=m.amount_residual, payment_state=m.payment_state,
        orders=m.line_ids.sale_line_ids.order_id.ids,
        lines=[(l.product_id.id, l.quantity, l.price_unit, l.tax_ids.ids, l.account_id.code) for l in m.invoice_line_ids]))
pays = [dict(id=p.id, partner=p.partner_id.id, amount=p.amount, date=str(p.date), journal=p.journal_id.code,
             invoices=p.reconciled_invoice_ids.mapped('name'), memo=p.memo) for p in env['account.payment'].sudo().search([])]
env.cr.execute("""select count(*), sum(debe), sum(haber), count(distinct partner_id),
    md5(string_agg(concat_ws('|', id, partner_id, fecha, comprobante, concepto, tipo, debe, haber, currency_id, origen_id), ',' order by id))
    from distri_cuenta_movimiento""")
n, debe, haber, partners, digest = env.cr.fetchone()
hist = dict(count=n, debe=float(debe), haber=float(haber), partners=partners, md5=digest)
json.dump(dict(db=env.cr.dbname, balances=out, moves=moves, payments=pays, history=hist,
               total=round(sum(out.values()), 2)),
          open('/scripts/out/snap_%s.json' % tag, 'w'), indent=1, default=str)
print("SNAP", tag, env.cr.dbname, "PARTNERS", len(out), "TOTAL", round(sum(out.values()), 2), "HISTORY", hist)
env.cr.rollback()
