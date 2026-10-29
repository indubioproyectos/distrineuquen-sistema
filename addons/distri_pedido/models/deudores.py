"""«Deudores»: who owes, highest debt first, and the PDF for management
(19.0.1.48.0).

- Direct customers only: commercial partners with sales (customer rank,
  receivable lines or imported history), without the generic web contacts
  and the resellers and wholesalers (revision_cliente.is_reseller).
- «Incobrable» (res.partner.distri_uncollectible, on the commercial
  partner): set by administración/gerencia. The customer leaves the debtor
  list and its totals and shows apart in «Deudas incobrables», with its
  balance and statement. Nothing is written off: the accounting stays as it
  is until the accountant decides.
- Seller tags: «Italo» (the seller's portfolio tag, found by name: it was
  created by hand) and «Distrineuquén» (data in views/deudores.xml). New
  direct customers get the portfolio tag of the user who creates them, else
  «Distrineuquén» (res.partner._distri_default_seller_tag).
- The screen is a snapshot (distri.debtor.report + distri.debtor.line,
  transient) of every debtor and every uncollectible customer, all sellers,
  built when it opens, when the cut date changes and with «Actualizar». The
  balances come from a few bulk reads (debtor_balances), not from the
  per-customer statement: same rule as the statement and as
  res.partner._distri_account_balance, and the tests check they agree.
- «Vendedor» and «Deuda mínima» only filter that snapshot (19.0.1.48.0):
  the lines shown, their position and the totals are computed, not stored,
  so a change shows at once (onchange) and the PDF, the Excel and the
  presets read exactly what is on screen. The form saves every filter change
  right away (static/src/js/deudores_form.js): the list comes back in debt
  order and a new cut date rebuilds the snapshot on the server.
- Cut date («Fecha de corte»): the balance as of that day is the running
  balance of the statement on that day (the closing of
  _distri_statement(False, cut)): every movement dated up to the cut,
  with the same deduplication against this Odoo as the full statement. The
  pending receipt candidates count only when dated up to the cut.
"""
import base64
import io
from collections import defaultdict
from datetime import date, datetime
from types import SimpleNamespace

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError
from odoo.tools.pdf import merge_pdf

from .ajuste_cuenta import AJC_JOURNAL_CODE
from .armado import normalizar_ciudad
from .cuenta import clave_documento
from .estado_cuenta import SOURCE_LABELS, _imported_staff_type, _money, local_date
from .reparto_cliente import AR_TZ, today_ar, town_from_name
from .revision_cliente import is_reseller

ADMIN_GROUP = "distri_pedido.grupo_administracion"
ITALO_TAG_NAME = "Italo"
DISTRI_TAG_XMLID = "distri_pedido.categoria_vendedor_distrineuquen"
UNCOLLECTIBLE_FIELDS = ("distri_uncollectible", "distri_uncollectible_date",
                        "distri_uncollectible_note")
# a balance above one cent is a debt
DEBT_EPSILON = 0.005

SELLERS = [
    ("italo", "Italo"),
    ("distrineuquen", "Distrineuquén"),
    ("none", "Sin vendedor"),
]
SELLER_FILTERS = [
    ("all", "Todos"),
    ("italo", "Clientes de Italo"),
    ("distrineuquen", "Clientes de Distrineuquén"),
]


def seller_tags(env):
    """(Italo tag, Distrineuquén tag); either may be empty."""
    Category = env["res.partner.category"].sudo()
    italo = Category.search([("name", "=", ITALO_TAG_NAME)], limit=1)
    distri = env.ref(DISTRI_TAG_XMLID, raise_if_not_found=False) or Category.browse()
    return italo, distri.sudo()


def seller_of(partner, tags=None):
    """'italo', 'distrineuquen' or 'none', from the commercial partner's tags
    (Italo wins when both are set)."""
    italo, distri = tags or seller_tags(partner.env)
    categories = partner.commercial_partner_id.sudo().category_id
    if italo and italo in categories:
        return "italo"
    if distri and distri in categories:
        return "distrineuquen"
    return "none"


def direct_customers(env):
    """Commercial partners that buy directly (archived ones too: a debt is a
    debt): customer rank or documents, not generic, not resellers."""
    cr = env.cr
    env["distri.cuenta.movimiento"].flush_model()
    env["account.move.line"].flush_model()
    cr.execute("SELECT DISTINCT partner_id FROM distri_cuenta_movimiento")
    with_docs = {row[0] for row in cr.fetchall()}
    cr.execute("""
        SELECT DISTINCT l.partner_id
          FROM account_move_line l JOIN account_account a ON a.id = l.account_id
         WHERE a.account_type = 'asset_receivable' AND l.parent_state = 'posted'
           AND l.partner_id IS NOT NULL""")
    with_docs |= {row[0] for row in cr.fetchall()}
    partners = env["res.partner"].sudo().with_context(active_test=False).search([
        ("parent_id", "=", False), ("distri_generic_partner", "=", False)])
    return partners.filtered(
        lambda p: (p.customer_rank > 0 or p.id in with_docs) and not is_reseller(p))


def debtor_balances(env, partners, date_to=False):
    """Balance of each commercial partner as of `date_to` (all when False),
    from bulk reads, with the statement's rule (models/estado_cuenta.py):
    Odoo receivable lines, the imported history not already in Odoo (same
    number and amount), minus the provisional returns.

    Returns {partner_id: {"balance", "last_payment": (date, amount) or None,
    "last_invoice": (date, amount) or None, "candidate_amount",
    "candidate_count"}}.
    """
    ids = partners.ids
    rows = defaultdict(list)  # partner_id -> [(day, kind, debit, credit, sort key)]
    seen = defaultdict(set)

    lines = env["account.move.line"].sudo().search_read([
        ("partner_id", "in", ids),
        ("parent_state", "=", "posted"),
        ("account_id.account_type", "=", "asset_receivable"),
    ], ["partner_id", "date", "debit", "credit", "move_name", "move_type", "journal_id"],
        order="date, id")
    journal_ids = {line["journal_id"][0] for line in lines if line["journal_id"]}
    codes = {j.id: j.code for j in env["account.journal"].sudo().browse(list(journal_ids))}
    for line in lines:
        pid = line["partner_id"][0]
        seen[pid].add(clave_documento(line["move_name"], line["debit"], line["credit"]))
        journal = line["journal_id"] and codes.get(line["journal_id"][0])
        if journal == AJC_JOURNAL_CODE:
            kind = "adjustment"
        elif line["move_type"] == "out_invoice":
            kind = "invoice"
        elif line["move_type"] == "out_refund":
            kind = "refund"
        elif line["credit"]:
            kind = "payment"
        else:
            kind = "other"
        rows[pid].append((line["date"], kind, line["debit"], line["credit"],
                          (1, line["id"])))
    for pid in seen:
        seen[pid].discard(None)

    history = env["distri.cuenta.movimiento"].sudo().search_read(
        [("partner_id", "in", ids)], ["partner_id", "fecha", "comprobante", "debe", "haber", "tipo"],
        order="fecha, id")
    for mov in history:
        pid = mov["partner_id"][0]
        debe, haber = mov["debe"] or 0.0, mov["haber"] or 0.0
        if clave_documento(mov["comprobante"], debe, haber) in seen[pid]:
            continue
        staff = _imported_staff_type(SimpleNamespace(
            comprobante=mov["comprobante"], debe=debe, haber=haber, tipo=mov["tipo"]))
        kind = {"Factura": "invoice", "Pago": "payment"}.get(staff, "other")
        rows[pid].append((mov["fecha"], kind, debe, haber, (0, mov["id"])))

    wanted = set(ids)
    returns = env["distri.route.return"].sudo().search([
        ("order_id.partner_id.commercial_partner_id", "in", ids), ("quantity", ">", 0)])
    for ret in returns.filtered(lambda r: r._distri_is_provisional()):
        pid = ret.order_id.partner_id.commercial_partner_id.id
        if pid not in wanted:
            continue
        when = local_date(ret.stop_id.visit_captured_at or ret.create_date) or today_ar()
        rows[pid].append((when, "return", 0.0, ret.amount, (2, ret.id)))

    candidates = defaultdict(lambda: [0, 0.0])
    cards = env["distri.mp.collection"].sudo().search([
        ("partner_id", "child_of", ids), ("kind", "=", "income"), ("state", "=", "draft"),
        ("receipt_candidate", "=", True)])
    for card in cards:
        if date_to and card.date_day and card.date_day > date_to:
            continue
        pid = card.partner_id.commercial_partner_id.id
        if pid in wanted:
            candidates[pid][0] += 1
            candidates[pid][1] += card.amount

    result = {}
    for pid in ids:
        balance = 0.0
        last_payment = last_invoice = None
        for day, kind, debit, credit, key in sorted(
                rows.get(pid, []), key=lambda r: (r[0] or date.min, r[4])):
            if date_to and (day or date.min) > date_to:
                break
            balance += (debit or 0.0) - (credit or 0.0)
            if kind == "payment":
                last_payment = (day, credit)
            elif kind == "invoice":
                last_invoice = (day, debit)
        count, amount = candidates.get(pid, (0, 0.0))
        result[pid] = {
            "balance": balance,
            "last_payment": last_payment,
            "last_invoice": last_invoice,
            "candidate_count": count,
            "candidate_amount": amount,
        }
    return result


class ResPartnerDeudores(models.Model):
    _inherit = "res.partner"

    distri_uncollectible = fields.Boolean(
        string="Incobrable", tracking=True, copy=False,
        help="La deuda se da por incobrable: el cliente sale de «Deudores» y de sus totales y "
             "aparece en «Deudas incobrables». No se borra ni se ajusta nada en la contabilidad. "
             "Lo marca administración o gerencia.")
    distri_uncollectible_date = fields.Date(string="Incobrable desde", tracking=True, copy=False)
    distri_uncollectible_note = fields.Char(string="Motivo (incobrable)", tracking=True,
                                            copy=False)

    def _distri_check_uncollectible_access(self, vals):
        if any(f in vals for f in UNCOLLECTIBLE_FIELDS) and not (
                self.env.su or self.env.user.has_group(ADMIN_GROUP)):
            raise AccessError("Solo administración y gerencia marcan un cliente como incobrable.")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._distri_check_uncollectible_access(vals)
        partners = super().create(vals_list)
        partners.filtered(lambda p: p.customer_rank > 0)._distri_default_seller_tag()
        return partners

    def write(self, vals):
        self._distri_check_uncollectible_access(vals)
        result = super().write(vals)
        # the first invoice (account _increase_rank) or a new customer made
        # from an order: it becomes a customer
        if vals.get("customer_rank"):
            self._distri_default_seller_tag()
        return result

    def _distri_default_seller_tag(self):
        """A new direct customer without a seller tag gets the portfolio tag
        of the user who makes it a customer (a seller's own «Cartera de
        clientes»), else «Distrineuquén». Never on contacts of a company,
        generic contacts, resellers or customers that already carry a seller
        or portfolio tag."""
        if self.env.context.get("distri_no_seller_tag") or not self:
            return
        italo, distri = seller_tags(self.env)
        Users = self.env["res.users"].sudo().with_context(active_test=False)
        portfolio_tags = Users.search([("etiqueta_cartera_id", "!=", False)]).mapped(
            "etiqueta_cartera_id")
        known = italo | distri | portfolio_tags
        default = self.env.user.etiqueta_cartera_id or distri
        if not default:
            return
        for partner in self.sudo():
            if (partner.parent_id or partner.distri_generic_partner
                    or partner.category_id & known or is_reseller(partner)):
                continue
            partner.write({"category_id": [Command.link(default.id)]})


class DistriDebtorReport(models.TransientModel):
    _name = "distri.debtor.report"
    _description = "Deudores"

    date_to = fields.Date(string="Fecha de corte",
                          help="Saldo de cada cliente a ese día, con los movimientos hasta esa "
                               "fecha. Vacío: hoy.")
    seller = fields.Selection(SELLER_FILTERS, string="Vendedor", default="all", required=True)
    min_amount = fields.Monetary(string="Deuda mínima")
    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)
    built_at = fields.Datetime(string="Calculado el", readonly=True)
    # the snapshot: every debtor and uncollectible customer, all sellers
    snapshot_line_ids = fields.One2many("distri.debtor.line", "report_id", readonly=True)
    # what the filters select from it
    line_ids = fields.One2many("distri.debtor.line", string="Deudores",
                               compute="_compute_selection")
    uncollectible_line_ids = fields.One2many(
        "distri.debtor.line", string="Deudas incobrables", compute="_compute_selection")
    debtor_count = fields.Integer(string="Clientes que deben", compute="_compute_selection")
    total_debt = fields.Monetary(string="Deuda total", compute="_compute_selection")
    total_italo = fields.Monetary(string="Clientes de Italo", compute="_compute_selection")
    total_distri = fields.Monetary(string="Clientes de Distrineuquén",
                                   compute="_compute_selection")
    total_none = fields.Monetary(string="Sin vendedor", compute="_compute_selection")
    total_candidates = fields.Monetary(
        string="Pagos a confirmar", compute="_compute_selection",
        help="Candidatos sin comprobante de los deudores: no restan de la deuda.")
    uncollectible_count = fields.Integer(string="Incobrables", compute="_compute_selection")
    uncollectible_total = fields.Monetary(
        string="Deuda incobrable", compute="_compute_selection",
        help="Aparte: no entra en la deuda total.")

    @api.depends("seller", "min_amount", "snapshot_line_ids.kind", "snapshot_line_ids.seller",
                 "snapshot_line_ids.balance", "snapshot_line_ids.candidate_amount")
    def _compute_selection(self):
        for report in self:
            minimum = report.min_amount or 0.0
            chosen = report.snapshot_line_ids.filtered(
                lambda l: report.seller == "all" or l.seller == report.seller)
            debtors = chosen.filtered(lambda l: l.kind == "debtor" and l.balance >= minimum)
            gone = chosen.filtered(lambda l: l.kind == "uncollectible")
            by_seller = defaultdict(float)
            for line in debtors:
                by_seller[line.seller] += line.balance
            report.line_ids = debtors
            report.uncollectible_line_ids = gone
            report.debtor_count = len(debtors)
            report.total_debt = round(sum(debtors.mapped("balance")), 2)
            report.total_italo = round(by_seller["italo"], 2)
            report.total_distri = round(by_seller["distrineuquen"], 2)
            report.total_none = round(by_seller["none"], 2)
            report.total_candidates = round(sum(debtors.mapped("candidate_amount")), 2)
            report.uncollectible_count = len(gone)
            report.uncollectible_total = round(sum(gone.mapped("balance")), 2)

    @api.model
    def _distri_check_access(self):
        if not (self.env.su or self.env.user.has_group(ADMIN_GROUP)):
            raise AccessError("Solo administración y gerencia.")

    @api.model_create_multi
    def create(self, vals_list):
        self._distri_check_access()
        reports = super().create(vals_list)
        reports._distri_build()
        return reports

    def write(self, vals):
        result = super().write(vals)
        # seller and minimum only filter the snapshot (_compute_selection)
        if "date_to" in vals:
            self._distri_build()
        return result

    @api.model
    def action_open(self):
        """Menu «Deudores»: a fresh snapshot."""
        report = self.create({})
        return report._distri_form_action()

    def _distri_form_action(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Deudores",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    # ------------------------------------------------------------ build

    def _distri_build(self):
        """The snapshot as of the cut date: every debtor (balance above one
        cent) and every uncollectible customer, of every seller. The
        positions are kept per kind, overall and within the seller, so any
        filter shows 1, 2, 3… without rebuilding (DistriDebtorLine.rank)."""
        self._distri_check_access()
        Line = self.env["distri.debtor.line"].sudo()
        customers = direct_customers(self.env)
        tags = seller_tags(self.env)
        sellers = {partner.id: seller_of(partner, tags) for partner in customers}
        zones = {normalizar_ciudad(z.name): z.name
                 for z in self.env["distri.zona"].sudo().search([])}
        for report in self:
            cut = report.date_to or False
            balances = debtor_balances(self.env, customers, cut)
            report.sudo().snapshot_line_ids.unlink()
            vals_list = []
            for partner in customers:
                data = balances[partner.id]
                if partner.distri_uncollectible:
                    kind = "uncollectible"
                elif data["balance"] > DEBT_EPSILON:
                    kind = "debtor"
                else:
                    continue
                town = partner.city or town_from_name(partner.name) or ""
                vals_list.append({
                    "_name": partner.name or "",
                    "report_id": report.id,
                    "kind": kind,
                    "partner_id": partner.id,
                    "seller": sellers[partner.id],
                    "balance": round(data["balance"], 2),
                    "last_payment_date": data["last_payment"] and data["last_payment"][0],
                    "last_payment_amount": data["last_payment"] and data["last_payment"][1],
                    "last_invoice_date": data["last_invoice"] and data["last_invoice"][0],
                    "last_invoice_amount": data["last_invoice"] and data["last_invoice"][1],
                    "candidate_count": data["candidate_count"],
                    "candidate_amount": data["candidate_amount"],
                    "phone": partner.distri_whatsapp or partner.phone or "",
                    "zone": zones.get(normalizar_ciudad(town), town),
                })
            vals_list.sort(key=lambda v: (-v["balance"], v["_name"]))
            rank_all = defaultdict(int)
            rank_seller = defaultdict(int)
            for vals in vals_list:
                del vals["_name"]
                rank_all[vals["kind"]] += 1
                rank_seller[vals["kind"], vals["seller"]] += 1
                vals["rank_all"] = rank_all[vals["kind"]]
                vals["rank_seller"] = rank_seller[vals["kind"], vals["seller"]]
            Line.create(vals_list)
            report.sudo().write({"built_at": fields.Datetime.now()})
        return True

    def action_refresh(self):
        self._distri_build()
        return True

    # ------------------------------------------------------------ exports

    def action_print_pdf(self):
        self.ensure_one()
        self._distri_check_access()
        return self.env.ref("distri_pedido.report_deudores").report_action(self)

    def _distri_preset(self, seller):
        self.ensure_one()
        self.write({"seller": seller})
        return self.action_print_pdf()

    def action_preset_italo(self):
        return self._distri_preset("italo")

    def action_preset_distri(self):
        return self._distri_preset("distrineuquen")

    def _distri_selection_label(self):
        self.ensure_one()
        return dict(SELLER_FILTERS)[self.seller]

    def _distri_statement_values(self, line):
        """The account statement of one debtor for the PDF, as of the cut:
        the customer's whole history up to that day, the receipt
        candidates up to that day shown and never subtracted."""
        cut = self.date_to or False
        partner = line.partner_id.sudo()
        data = dict(partner._distri_statement(False, cut))
        candidates = [c for c in data["candidates"]
                      if not (cut and c["date"] and c["date"] > cut)]
        amount = sum(c["amount"] for c in candidates)
        data.update({
            "balance": data["closing"],
            "candidates": candidates,
            "candidate_amount": amount,
            "whatif_balance": data["closing"] - amount,
        })
        return {
            "partner": partner,
            "date_from": False,
            "date_to": cut,
            "printed": self._distri_issue_date(),
            "data": data,
            "money": _money,
            "sources": SOURCE_LABELS,
            "balance_label": "Saldo al %s" % cut.strftime("%d/%m/%Y") if cut else "Saldo actual",
            "candidate_rows": True,
            "uncollectible": partner.distri_uncollectible,
        }

    def _distri_issue_date(self):
        return datetime.now(AR_TZ).strftime("%d/%m/%Y")

    def _report_values(self):
        self.ensure_one()
        self._distri_check_access()
        lines = self.line_ids.sorted("rank")
        # 19.0.1.53.0: the PDF goes in parts (ReportDeudoresParts): «summary»
        # is the first page alone, (start, end) only those statements
        part = self.env.context.get("distri_debtor_part")
        statements = lines
        if part == "summary":
            statements = lines.browse()
        elif part:
            statements = lines[part[0]:part[1]]
        return {
            "report": self,
            "issued": self._distri_issue_date(),
            "cut": (self.date_to or today_ar()).strftime("%d/%m/%Y"),
            "selection": self._distri_selection_label(),
            "lines": lines,
            "uncollectible": self.uncollectible_line_ids.sorted("rank"),
            "sellers": dict(SELLERS),
            "money": _money,
            "only_statements": bool(part) and part != "summary",
            "statements": [self._distri_statement_values(line) for line in statements],
        }

    def action_export_xlsx(self):
        self.ensure_one()
        self._distri_check_access()
        import xlsxwriter

        buffer = io.BytesIO()
        book = xlsxwriter.Workbook(buffer, {"in_memory": True})
        bold = book.add_format({"bold": True})
        money = book.add_format({"num_format": "#,##0.00"})
        money_bold = book.add_format({"num_format": "#,##0.00", "bold": True})
        day = book.add_format({"num_format": "dd/mm/yyyy"})
        head = ["Puesto", "Cliente", "Referencia", "Vendedor", "Deuda", "Último pago",
                "Importe último pago", "Última factura", "Importe última factura",
                "Pagos a confirmar", "Teléfono", "Zona"]
        sellers = dict(SELLERS)

        def sheet(name, lines, total_label):
            ws = book.add_worksheet(name)
            ws.write_row(0, 0, head, bold)
            row = 1
            for line in lines:
                ws.write(row, 0, line.rank)
                ws.write(row, 1, line.partner_id.name or "")
                ws.write(row, 2, line.partner_id.ref or "")
                ws.write(row, 3, sellers.get(line.seller, ""))
                ws.write_number(row, 4, line.balance, money)
                if line.last_payment_date:
                    ws.write_datetime(row, 5, datetime.combine(line.last_payment_date,
                                                               datetime.min.time()), day)
                    ws.write_number(row, 6, line.last_payment_amount, money)
                if line.last_invoice_date:
                    ws.write_datetime(row, 7, datetime.combine(line.last_invoice_date,
                                                               datetime.min.time()), day)
                    ws.write_number(row, 8, line.last_invoice_amount, money)
                ws.write_number(row, 9, line.candidate_amount, money)
                ws.write(row, 10, line.phone or "")
                ws.write(row, 11, line.zone or "")
                row += 1
            ws.write(row, 1, total_label, bold)
            ws.write_number(row, 4, sum(lines.mapped("balance")), money_bold)
            ws.write_number(row, 9, sum(lines.mapped("candidate_amount")), money_bold)
            ws.set_column(1, 1, 40)
            ws.set_column(2, 11, 14)

        sheet("Deudores", self.line_ids.sorted("rank"), "Total")
        sheet("Deudas incobrables", self.uncollectible_line_ids.sorted("rank"),
              "Total incobrable (aparte)")
        book.close()
        cut = (self.date_to or today_ar()).strftime("%Y-%m-%d")
        attachment = self.env["ir.attachment"].create({
            "name": "Deudores %s - %s.xlsx" % (self._distri_selection_label(), cut),
            "datas": base64.b64encode(buffer.getvalue()),
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        })
        return {"type": "ir.actions.act_url", "target": "self",
                "url": "/web/content/%s?download=true" % attachment.id}


class DistriDebtorLine(models.TransientModel):
    _name = "distri.debtor.line"
    _description = "Deudor"
    _order = "rank_all, id"

    report_id = fields.Many2one("distri.debtor.report", required=True, ondelete="cascade",
                                index=True)
    kind = fields.Selection([("debtor", "Deudor"), ("uncollectible", "Incobrable")],
                            required=True, default="debtor")
    # position in the snapshot (per kind), overall and within the seller;
    # `rank` is the one the report's seller filter shows
    rank_all = fields.Integer(string="Puesto (todos)")
    rank_seller = fields.Integer(string="Puesto (vendedor)")
    rank = fields.Integer(string="Puesto", compute="_compute_rank")
    partner_id = fields.Many2one("res.partner", string="Cliente", required=True,
                                 ondelete="cascade")
    partner_ref = fields.Char(related="partner_id.ref", string="Referencia")
    seller = fields.Selection(SELLERS, string="Vendedor")
    currency_id = fields.Many2one(related="report_id.currency_id")
    balance = fields.Monetary(string="Deuda")
    last_payment_date = fields.Date(string="Último pago")
    last_payment_amount = fields.Monetary(string="Importe último pago")
    last_invoice_date = fields.Date(string="Última factura")
    last_invoice_amount = fields.Monetary(string="Importe última factura")
    candidate_count = fields.Integer(string="Candidatos")
    candidate_amount = fields.Monetary(
        string="Pagos a confirmar",
        help="Candidatos sin comprobante: se muestran, no restan de la deuda.")
    phone = fields.Char(string="Teléfono")
    zone = fields.Char(string="Zona")
    uncollectible_date = fields.Date(related="partner_id.distri_uncollectible_date")
    uncollectible_note = fields.Char(related="partner_id.distri_uncollectible_note")

    @api.depends("report_id.seller", "rank_all", "rank_seller")
    def _compute_rank(self):
        for line in self:
            line.rank = line.rank_all if line.report_id.seller == "all" else line.rank_seller

    def action_statement(self):
        """The customer's statement up to the cut date."""
        self.ensure_one()
        wizard = self.env["distri.estado.cuenta"].create({
            "partner_id": self.partner_id.id, "date_to": self.report_id.date_to or False})
        return {
            "type": "ir.actions.act_window",
            "name": "Estado de cuenta de %s" % self.partner_id.name,
            "res_model": "distri.estado.cuenta",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }


class ReportDeudores(models.AbstractModel):
    _name = "report.distri_pedido.deudores_documento"
    _description = "Deudores (PDF)"

    @api.model
    def _get_report_values(self, docids, data=None):
        reports = self.env["distri.debtor.report"].browse(docids)
        return {
            "doc_ids": docids,
            "doc_model": "distri.debtor.report",
            "docs": reports,
            "values": {r.id: r._report_values() for r in reports},
        }


class ReportDeudoresParts(models.Model):
    """19.0.1.53.0: the debtors PDF with every statement (about 300 pages,
    6 MB of HTML) crashed wkhtmltopdf (error -11). It is rendered as the
    summary plus groups of STATEMENTS_PER_PART statements, each its own
    wkhtmltopdf run, merged in order into one PDF."""
    _inherit = "ir.actions.report"

    STATEMENTS_PER_PART = 25

    def _render_qweb_pdf(self, report_ref, res_ids=None, data=None):
        report = self._get_report(report_ref)
        if isinstance(res_ids, int):
            res_ids = [res_ids]
        if (report.report_name != "distri_pedido.deudores_documento"
                or self.env.context.get("distri_debtor_part") or not res_ids or len(res_ids) != 1):
            return super()._render_qweb_pdf(report_ref, res_ids=res_ids, data=data)
        debtors = self.env["distri.debtor.report"].browse(res_ids)
        total = len(debtors.line_ids)
        parts = ["summary"] + [(start, min(start + self.STATEMENTS_PER_PART, total))
                               for start in range(0, total, self.STATEMENTS_PER_PART)]
        pdfs = []
        for part in parts:
            pdf, _kind = super(ReportDeudoresParts, self.with_context(
                distri_debtor_part=part, report_pdf_no_attachment=True))._render_qweb_pdf(
                report_ref, res_ids=res_ids, data=dict(data or {}))
            pdfs.append(pdf)
        return merge_pdf(pdfs), "pdf"


class DistriBalanceReviewSeller(models.Model):
    _inherit = "distri.balance.review"

    # «Revisión por cliente»: filter and group by seller; the order stays
    # highest debt first (lab_balance desc)
    seller = fields.Selection(SELLERS, string="Vendedor", compute="_compute_seller", store=True,
                              index=True)

    @api.depends("partner_id.category_id")
    def _compute_seller(self):
        tags = seller_tags(self.env)
        for review in self:
            review.seller = seller_of(review.partner_id, tags) if review.partner_id else "none"
