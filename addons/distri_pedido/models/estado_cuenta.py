"""Account statement of a customer: the whole history, one list.

One statement merges three sources, sorted by date with a running balance,
from the first movement to today:

- the history imported from Odoo Online (distri.cuenta.movimiento);
- the documents in this Odoo: posted receivable lines (invoices, credit
  notes, payments);
- the returns the driver reported (route visit or depot delivery) that are
  not settled yet: «Devolución informada», marked «a controlar». They lower
  the balance right away; at settlement the posted credit note replaces
  them (distri.route.return._distri_is_provisional).

A document present both in the history and in Odoo counts once and the
live one from Odoo wins (models/cuenta.py, clave_documento: number plus
amount). res.partner._distri_account_balance applies the same rule and
always equals the closing balance of the full statement.

Staff see the invoice number and the remito. The customer sees only the
remito (models/remito.py): `customer_type` and `customer_ref` never carry
an invoice number.
"""
from datetime import date, datetime, timezone

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_escape

from .ajuste_cuenta import AJC_JOURNAL_CODE
from .cuenta import clave_documento
from .remito import es_numero_de_factura
from .reparto_cliente import AR_TZ, today_ar

VE_SALDOS = (
    "distri_pedido.grupo_vendedor,"
    "distri_pedido.grupo_administracion,"
    "distri_pedido.grupo_gerencia"
)

# Sort order inside a day: the Online history first, then this Odoo, then
# the provisional returns (the newest information).
SOURCE_ONLINE = "online"
SOURCE_ODOO = "odoo"
SOURCE_RETURN = "return"
_SOURCE_ORDER = {SOURCE_ONLINE: 0, SOURCE_ODOO: 1, SOURCE_RETURN: 2}
SOURCE_LABELS = {
    SOURCE_ONLINE: "Odoo Online",
    SOURCE_ODOO: "Odoo",
    SOURCE_RETURN: "Reparto",
}

PROVISIONAL_LABEL = "Devolución informada"
PROVISIONAL_NOTE = "a controlar"


def local_date(moment):
    """Naive UTC datetime -> date in Argentina."""
    if not moment:
        return False
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ).date()


def imported_customer_view(mov):
    """How the customer sees an imported movement: (type, reference).

    The history keeps the Odoo Online invoice or credit note number
    (0001/AAAA/NNNNN), which the customer never sees: it shows as «Remito»
    or «Nota de crédito» without a number. Payments keep their receipt
    number.
    """
    number = mov.comprobante or ""
    if es_numero_de_factura(number):
        kind = "Remito" if (mov.debe or 0.0) >= (mov.haber or 0.0) else "Nota de crédito"
        return kind, ""
    if mov.tipo in ("factura", "pago"):
        # bank and cash statements (BNK1/..., CSH1/...) and receipts
        return ("Pago" if mov.haber else "Ajuste"), number
    if mov.tipo == "nota":
        return "Nota de crédito", ""
    return "Movimiento", number


def _imported_staff_type(mov):
    number = mov.comprobante or ""
    if es_numero_de_factura(number):
        return "Factura" if (mov.debe or 0.0) >= (mov.haber or 0.0) else "Nota de crédito"
    if mov.tipo == "nota":
        return "Nota de crédito"
    if mov.haber and not mov.debe:
        return "Pago"
    return "Ajuste" if mov.tipo != "otro" else "Movimiento"


def _money(amount):
    """$ 62.300 / $ 62.300,50 / -$ 1.200"""
    sign = "-" if (amount or 0.0) < -0.004 else ""
    text = "{:,.2f}".format(abs(amount or 0.0)).replace(",", "X").replace(".", ",").replace("X", ".")
    if text.endswith(",00"):
        text = text[:-3]
    return "%s$ %s" % (sign, text)


class ResPartnerEstadoCuenta(models.Model):
    _inherit = "res.partner"

    distri_statement_html = fields.Html(
        string="Estado de cuenta", compute="_compute_distri_statement_html",
        sanitize=False, groups=VE_SALDOS)
    distri_statement_balance = fields.Monetary(
        string="Saldo de la cuenta", compute="_compute_distri_statement_html",
        currency_field="currency_id", groups=VE_SALDOS,
        help="Historia de Odoo Online más lo registrado en este Odoo y las "
             "devoluciones informadas. Positivo: debe. Negativo: saldo a favor.")

    # ------------------------------------------------------------ rows

    def _distri_statement_rows(self):
        """Every movement of the customer, oldest first, with running balance.

        Each row: date, source, doc_type (staff), number (staff: invoice or
        payment number), remito, concept, customer_type, customer_ref,
        debit, credit, balance, provisional, key (stable sort key).
        """
        self.ensure_one()
        partner = self.commercial_partner_id.sudo()
        rows = []

        lines = self.env["account.move.line"].sudo().search([
            ("partner_id", "=", partner.id),
            ("parent_state", "=", "posted"),
            ("account_id.account_type", "=", "asset_receivable"),
        ], order="date, id")
        seen = set()
        for line in lines:
            move = line.move_id
            seen.add(clave_documento(move.name, line.debit, line.credit))
            if move.journal_id.code == AJC_JOURNAL_CODE:
                # «Ajuste de cuentas» (19.0.1.44.0): neither a sale nor a return
                doc_type = customer_type = "Ajuste de cuenta"
                remito = customer_ref = ""
            elif move.move_type == "out_invoice":
                doc_type, customer_type = "Factura", "Remito"
                remito = customer_ref = move._distri_remito_texto()
            elif move.move_type == "out_refund":
                doc_type, customer_type = "Nota de crédito", "Devolución"
                remito = customer_ref = move._distri_remito_texto()
            elif line.credit:
                doc_type = customer_type = "Pago"
                remito, customer_ref = "", move.name or ""
            else:
                doc_type = customer_type = "Ajuste"
                remito = customer_ref = ""
            rows.append({
                "date": line.date,
                "source": SOURCE_ODOO,
                "doc_type": doc_type,
                "number": move.name or "",
                "remito": remito,
                "concept": move.ref or line.name or "",
                "customer_type": customer_type,
                "customer_ref": customer_ref,
                "debit": line.debit,
                "credit": line.credit,
                "provisional": False,
                "res_model": "account.move",
                "res_id": move.id,
                "key": (line.date, _SOURCE_ORDER[SOURCE_ODOO], line.id),
            })
        seen.discard(None)

        history = self.env["distri.cuenta.movimiento"].sudo().search(
            [("partner_id", "=", partner.id)], order="fecha, id")
        for mov in history:
            if clave_documento(mov.comprobante, mov.debe, mov.haber) in seen:
                continue
            customer_type, customer_ref = imported_customer_view(mov)
            rows.append({
                "date": mov.fecha,
                "source": SOURCE_ONLINE,
                "doc_type": _imported_staff_type(mov),
                "number": mov.comprobante or "",
                "remito": "",
                "concept": mov.concepto or "",
                "customer_type": customer_type,
                "customer_ref": customer_ref,
                "debit": mov.debe or 0.0,
                "credit": mov.haber or 0.0,
                "provisional": False,
                "res_model": "distri.cuenta.movimiento",
                "res_id": mov.id,
                "key": (mov.fecha, _SOURCE_ORDER[SOURCE_ONLINE], mov.id),
            })

        for ret in self.env["distri.route.return"]._distri_provisional_for(partner):
            when = local_date(ret.stop_id.visit_captured_at or ret.create_date) or today_ar()
            remitos = ret.order_id._distri_remitos_texto() or ret.order_id.name
            rows.append({
                "date": when,
                "source": SOURCE_RETURN,
                "doc_type": PROVISIONAL_LABEL,
                "number": "%s · %s" % (ret.order_id.name, ret.route_id.name or ""),
                "remito": remitos,
                "concept": "%g %s%s · %s" % (
                    ret.quantity, ret.product_id.name or "",
                    (" (%s)" % ret.reason) if ret.reason else "", PROVISIONAL_NOTE),
                "customer_type": PROVISIONAL_LABEL,
                "customer_ref": "%s · %s" % (remitos, PROVISIONAL_NOTE),
                "debit": 0.0,
                "credit": ret.amount,
                "provisional": True,
                "res_model": "distri.route.return",
                "res_id": ret.id,
                "key": (when, _SOURCE_ORDER[SOURCE_RETURN], ret.id),
            })

        rows.sort(key=lambda r: (r["key"][0] or date.min,) + r["key"][1:])
        balance = 0.0
        for row in rows:
            balance += (row["debit"] or 0.0) - (row["credit"] or 0.0)
            row["balance"] = balance
        return rows

    def _distri_statement(self, date_from=False, date_to=False):
        """Rows between two dates (inclusive) plus opening and closing balance."""
        self.ensure_one()
        rows = self._distri_statement_rows()
        opening = 0.0
        selected = []
        for row in rows:
            day = row["date"] or date.min
            if date_from and day < date_from:
                opening = row["balance"]
                continue
            if date_to and day > date_to:
                continue
            selected.append(row)
        closing = selected[-1]["balance"] if selected else opening
        return {
            "rows": selected,
            "opening": opening,
            "closing": closing,
            "balance": rows[-1]["balance"] if rows else 0.0,
            "provisional": sum(r["credit"] for r in rows if r["provisional"]),
            "debit": sum(r["debit"] for r in selected),
            "credit": sum(r["credit"] for r in selected),
        }

    # ------------------------------------------------------------ backend

    @api.depends_context("uid")
    def _compute_distri_statement_html(self):
        for partner in self:
            if not partner.id or partner.distri_generic_partner:
                partner.distri_statement_html = False
                partner.distri_statement_balance = 0.0
                continue
            data = partner._distri_statement()
            partner.distri_statement_balance = data["balance"]
            partner.distri_statement_html = partner._distri_statement_table(
                data, newest_first=True)

    def _distri_statement_table(self, data, newest_first=False):
        """Staff table: invoice number and remito, running balance."""
        rows = list(reversed(data["rows"])) if newest_first else data["rows"]
        if not rows:
            return "<p class='text-muted'>Sin movimientos.</p>"
        out = [
            "<div class='o_distri_estado'>",
            "<p><b>Saldo: %s</b>%s</p>" % (
                html_escape(_money(data["balance"])),
                (" · incluye devoluciones informadas a controlar por %s"
                 % html_escape(_money(data["provisional"]))) if data["provisional"] else ""),
            "<table class='table table-sm table-striped o_distri_estado_tabla'>",
            "<thead><tr><th>Fecha</th><th>Tipo</th><th>Comprobante</th><th>Remito</th>"
            "<th>Concepto</th><th>Origen</th><th class='text-end'>Debe</th>"
            "<th class='text-end'>Haber</th><th class='text-end'>Saldo</th></tr></thead><tbody>",
        ]
        for row in rows:
            out.append(
                "<tr%s><td>%s</td><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                "<td class='text-end'>%s</td><td class='text-end'>%s</td>"
                "<td class='text-end'>%s</td></tr>" % (
                    " class='table-warning'" if row["provisional"] else "",
                    row["date"].strftime("%d/%m/%Y") if row["date"] else "",
                    html_escape(row["doc_type"]),
                    " <span class='badge text-bg-warning'>a controlar</span>"
                    if row["provisional"] else "",
                    html_escape(row["number"]), html_escape(row["remito"]),
                    html_escape(row["concept"]), html_escape(SOURCE_LABELS[row["source"]]),
                    html_escape(_money(row["debit"])) if row["debit"] else "",
                    html_escape(_money(row["credit"])) if row["credit"] else "",
                    html_escape(_money(row["balance"]))))
        out.append("</tbody></table></div>")
        return "".join(out)

    def action_distri_estado_cuenta(self):
        """«Estado de cuenta»: the statement with date filters and printing."""
        self.ensure_one()
        wizard = self.env["distri.estado.cuenta"].create({
            "partner_id": self.commercial_partner_id.id})
        return {
            "type": "ir.actions.act_window",
            "name": "Estado de cuenta de %s" % self.commercial_partner_id.name,
            "res_model": "distri.estado.cuenta",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }


class DistriEstadoCuenta(models.TransientModel):
    _name = "distri.estado.cuenta"
    _description = "Estado de cuenta del cliente"

    partner_id = fields.Many2one("res.partner", string="Cliente", required=True,
                                 readonly=True)
    date_from = fields.Date(string="Desde")
    date_to = fields.Date(string="Hasta")
    currency_id = fields.Many2one("res.currency",
                                  default=lambda self: self.env.company.currency_id)
    opening = fields.Monetary(string="Saldo anterior", compute="_compute_statement")
    closing = fields.Monetary(string="Saldo al cierre", compute="_compute_statement")
    balance = fields.Monetary(string="Saldo de hoy", compute="_compute_statement")
    statement_html = fields.Html(string="Movimientos", compute="_compute_statement",
                                 sanitize=False)

    def _check_access_saldos(self):
        user = self.env.user
        if not (self.env.su or any(user.has_group(g) for g in VE_SALDOS.split(","))):
            raise UserError("Solo vendedores, administración y gerencia ven el estado de cuenta.")

    @api.model_create_multi
    def create(self, vals_list):
        self._check_access_saldos()
        return super().create(vals_list)

    @api.depends("partner_id", "date_from", "date_to")
    def _compute_statement(self):
        for wizard in self:
            if not wizard.partner_id:
                wizard.update({"opening": 0.0, "closing": 0.0, "balance": 0.0,
                               "statement_html": False})
                continue
            data = wizard._statement()
            wizard.opening = data["opening"]
            wizard.closing = data["closing"]
            wizard.balance = data["balance"]
            wizard.statement_html = wizard.partner_id.sudo()._distri_statement_table(
                data, newest_first=False)

    def _statement(self):
        self.ensure_one()
        self._check_access_saldos()
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError("La fecha «Desde» es posterior a «Hasta».")
        return self.partner_id.sudo()._distri_statement(self.date_from, self.date_to)

    def _report_values(self):
        """What the PDF prints."""
        self.ensure_one()
        data = self._statement()
        return {
            "partner": self.partner_id,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "printed": datetime.now(AR_TZ).strftime("%d/%m/%Y %H:%M"),
            "data": data,
            "money": _money,
            "sources": SOURCE_LABELS,
        }

    def action_print(self):
        self.ensure_one()
        self._statement()
        return self.env.ref("distri_pedido.report_estado_cuenta").report_action(self)


class ReportEstadoCuenta(models.AbstractModel):
    _name = "report.distri_pedido.estado_cuenta_documento"
    _description = "Estado de cuenta (PDF)"

    @api.model
    def _get_report_values(self, docids, data=None):
        wizards = self.env["distri.estado.cuenta"].browse(docids)
        return {
            "doc_ids": docids,
            "doc_model": "distri.estado.cuenta",
            "docs": wizards,
            "values": {w.id: w._report_values() for w in wizards},
        }
