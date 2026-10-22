# Converts the copy to Odoo Online's setup (26/09/2026):
#   - chart ar_base  -> company "Responsable Monotributo"
#   - sales journal "Ventas Preimpreso", code 0001, POS 1, WITHOUT ARCA
#     document types (numbers 0001/YYYY/NNNNN; credit notes share the series)
#   - Banco (BNK1) and Efectivo (CSH1) journals
#   - no taxes on invoices (IVA 0 like Online): no default sale tax, no taxes
#     on products nor on open order lines
#   - the lab test documents (invoices + payments) are replayed from their
#     sale orders, numbered after Online's last number (NEXT_NUMBER).
# Needs l10n_ar installed and /scripts/out/snap_before.json (snap.py).
# Commits only with DO_COMMIT=1.
import json, os
snap = json.load(open('/scripts/out/snap_before.json'))
NEXT_NUMBER = os.environ.get('NEXT_NUMBER') or '0001/2026/06276'  # Online: last 0001/2026/06275 (26/09)
company = env.company
Move = env['account.move'].sudo()
Payment = env['account.payment'].sudo()
SO = env['sale.order'].sudo()

# --- 1. what the test documents are attached to
auto = {so.id: so.distri_factura_auto_id.id for so in SO.search([('distri_factura_auto_id', '!=', False)])}
routes = {r.id: r.journal_id.code for r in env['distri.route'].sudo().search([])}
print("auto invoices", auto, "routes", routes)
assert len(snap['moves']) == Move.search_count([]), "the copy changed after the snapshot"

# --- 2. remove the lab-test moves (payments first); Odoo only replaces a
#        chart when the company has no journal items (_existing_accounting)
pays = Payment.search([])
pays.filtered(lambda p: p.state != 'draft').action_draft()
pays.with_context(force_delete=True).unlink()
invs = Move.search([], order='id desc')
invs.filtered(lambda m: m.state == 'posted').button_draft()
invs.with_context(force_delete=True).unlink()
assert not company._existing_accounting(), "still accounting"

# --- 3. Argentine chart for Monotributo (Odoo's supported loader)
env['account.chart.template'].try_loading('ar_base', company, install_demo=False)
env.invalidate_all()
company = env['res.company'].browse(company.id)
assert company.chart_template == 'ar_base', company.chart_template
assert company.l10n_ar_afip_responsibility_type_id == env.ref('l10n_ar.res_RM')
print("chart", company.chart_template, "resp", company.l10n_ar_afip_responsibility_type_id.name,
      "country", company.country_id.code, "fiscal", company.account_fiscal_country_id.code,
      "currency", company.currency_id.name, "rounding", company.tax_calculation_rounding_method)
Journal = env['account.journal'].sudo()
for j in Journal.search([]):
    print("J", j.id, j.code, j.type, j.name, j.l10n_latam_use_documents)

# --- 4. journals like Online
ventas = Journal.search([('type', '=', 'sale'), ('company_id', '=', company.id)])
assert len(ventas) == 1 and ventas.code == '0001', ventas.mapped('code')
ventas.write({'l10n_latam_use_documents': False, 'refund_sequence': False})
for lang in ('es_ES', 'es_AR', 'en_US'):
    ventas.with_context(lang=lang).name = 'Ventas Preimpreso'
banco = Journal.search([('type', '=', 'bank'), ('company_id', '=', company.id)], limit=1)
efectivo = Journal.search([('type', '=', 'cash'), ('company_id', '=', company.id)], limit=1)
if not efectivo:
    efectivo = Journal.create({'name': 'Efectivo', 'type': 'cash', 'code': 'CSH1', 'company_id': company.id})
for lang in ('es_ES', 'es_AR'):
    efectivo.with_context(lang=lang).name = 'Efectivo'
    banco.with_context(lang=lang).name = 'Banco'
for r in env['distri.route'].sudo().search([('state', '!=', 'settled')]):
    r.journal_id = efectivo

# --- 5. no taxes on invoices (Online: amount_tax = 0 on all 10,604 documents)
company.account_sale_tax_id = False
env['product.template'].sudo().with_context(active_test=False).search(
    [('taxes_id', '!=', False)]).write({'taxes_id': [(5, 0, 0)]})
open_lines = env['sale.order.line'].sudo().search([('tax_ids', '!=', False)])
open_lines.write({'tax_ids': [(5, 0, 0)]})
# replacing the chart deletes the generic 15% tax straight in SQL: the stored
# totals of the order lines that had it still include it. Recompute them.
stale = env['sale.order.line'].sudo().search([]).filtered(lambda l: not l.tax_ids and l.price_tax)
stale._compute_amount()
stale.order_id._compute_amounts()
env.flush_all()
print("orders with stale tax recomputed", stale.order_id.mapped('name'))
print("sales journal", ventas.code, ventas.name, "documents", ventas.l10n_latam_use_documents,
      "refund_sequence", ventas.refund_sequence, "| Banco", banco.code, "| Efectivo", efectivo.code,
      efectivo.default_account_id.code, efectivo.default_account_id.name,
      "| receivable", env['res.partner'].browse(54).property_account_receivable_id.code,
      "| income", company.income_account_id.code, company.income_account_id.name,
      "| default sale tax", company.account_sale_tax_id.name or '-',
      "| order lines untaxed", len(open_lines))

# --- 6. replay the lab-test documents from their sale orders
new_by_old = {}
first = True
for m in [m for m in snap['moves'] if m['type'] == 'out_invoice']:
    order = SO.browse(m['orders'][0])
    inv = order._create_invoices()
    inv.write({'invoice_date': m['date'], 'date': m['date']})
    if first:
        # continue Online's series (invoices and credit notes share it)
        inv.name = NEXT_NUMBER
        first = False
    inv.action_post()
    assert inv.amount_tax == 0 and not inv.invoice_line_ids.tax_ids, inv.name
    new_by_old[m['name']] = inv
    print("REPLAY", m['name'], '->', inv.name, order.name, 'old', m['total'], 'new', inv.amount_total)
    if order.id in auto:
        order.distri_factura_auto_id = inv
for p in sorted(snap['payments'], key=lambda p: p['id']):
    pay = Payment.create({'payment_type': 'inbound', 'partner_type': 'customer', 'partner_id': p['partner'],
                          'amount': p['amount'], 'date': p['date'], 'journal_id': banco.id,
                          'memo': new_by_old[p['invoices'][0]].name if p['invoices'] else p['memo']})
    pay.action_post()
    for old in p['invoices']:
        inv = new_by_old[old]
        lines = (pay.move_id.line_ids + inv.line_ids).filtered(
            lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
        lines.reconcile()
    print("PAY", pay.name, pay.amount, pay.state, [i.name for i in pay.reconciled_invoice_ids])

# --- 7. every customer balance against the snapshot. The only allowed
#        difference is a replayed lab-test invoice that carried the generic
#        chart's 15% tax: without IVA it is lower by exactly that tax.
env.invalidate_all()
expected = {}
for m in snap['moves']:
    if m['type'] == 'out_invoice' and m['tax']:
        key = str(m['partner'])
        expected[key] = round(expected.get(key, 0.0) - m['tax'], 2)
diffs = {}
for pid, before in snap['balances'].items():
    after = round(env['res.partner'].browse(int(pid))._distri_account_balance(), 2)
    if abs(after - before) > 0.001:
        diffs[pid] = round(after - before, 2)
print("BALANCE DIFFS", diffs, "EXPECTED (lab test tax only)", expected)
assert diffs == expected, (diffs, expected)
if os.environ.get('DO_COMMIT'):
    env.cr.commit()
    print("COMMITTED")
else:
    env.cr.rollback()
    print("DRY RUN, rolled back")
