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

The cards «Candidato · falta el comprobante» of the customer (19.0.1.45.0)
show apart for staff only, as «Pago a confirmar · falta el comprobante»:
never subtracted from the balance, with the balance if they were approved.

Staff see the invoice number and the remito. The customer sees only the
remito (models/remito.py): `customer_type` and `customer_ref` never carry
an invoice number.
"""
from datetime import date, datetime, timezone

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
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
CANDIDATE_ROW_LABEL = "Pago a confirmar · falta el comprobante"
WHATIF_LABEL = "Saldo si se aprueban los candidatos"


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


def statement_line_key(kind, record_id, nth=1):
    """Stable identity of a statement row, kept by the comments of the phone
    app (distri.account.comment.line_key). The sort key (`key`) is not
    enough: it carries the date and, for this Odoo, the receivable line id,
    which a reset to draft can recreate. Instead:

    - this Odoo: «account.move:<move id>» (a move keeps its id; a second
      receivable line of the same move adds «:2», «:3»…);
    - Odoo Online history: «online:<origen_id>», the id in Odoo Online, so a
      new import of the history keeps it; «distri.cuenta.movimiento:<id>»
      for a row without it;
    - provisional returns: «distri.route.return:<id>».
    """
    key = "%s:%d" % (kind, record_id)
    return key if nth <= 1 else "%s:%d" % (key, nth)


def _statement_number(row):
    """The row's number; a payment from a Mercado Pago card links to the card."""
    number = html_escape(row["number"])
    if not row.get("card_id"):
        return number
    return ("%s <a href='/odoo/distri.mp.collection/%d' target='_blank' "
            "class='o_distri_estado_tarjeta'>Ver tarjeta</a>" % (number, row["card_id"]))


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

    def _distri_candidate_cards(self):
        """The pending Mercado Pago cards of the customer marked «Candidato ·
        falta el comprobante» (with or without the receipt arrived yet)."""
        self.ensure_one()
        return self.env["distri.mp.collection"].sudo().search([
            ("partner_id", "child_of", self.commercial_partner_id.id),
            ("kind", "=", "income"), ("state", "=", "draft"),
            ("receipt_candidate", "=", True)], order="date, id")

    def _distri_statement_rows(self):
        """Every movement of the customer, oldest first, with running balance.

        Each row: date, source, doc_type (staff), number (staff: invoice or
        payment number), remito, concept, customer_type, customer_ref,
        debit, credit, balance, provisional, key (sort key), line_key
        (stable identity of the row, see statement_line_key).
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
        per_move = {}
        for line in lines:
            move = line.move_id
            per_move[move.id] = per_move.get(move.id, 0) + 1
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
                "owed": 0.0,
                "owed_note": "",
                "res_model": "account.move",
                "res_id": move.id,
                "key": (line.date, _SOURCE_ORDER[SOURCE_ODOO], line.id),
                "line_key": statement_line_key("account.move", move.id, per_move[move.id]),
            })
        seen.discard(None)

        history = self.env["distri.cuenta.movimiento"].sudo().with_context(active_test=True).search(
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
                # «Adeudada (revisada)»: the amount still owed, set by the review
                "owed": (mov.distri_owed_amount or mov.debe or 0.0) if mov.distri_owed else 0.0,
                "owed_note": (mov.distri_owed_note or "") if mov.distri_owed else "",
                "res_model": "distri.cuenta.movimiento",
                "res_id": mov.id,
                "key": (mov.fecha, _SOURCE_ORDER[SOURCE_ONLINE], mov.id),
                "line_key": (statement_line_key("online", mov.origen_id) if mov.origen_id
                             else statement_line_key("distri.cuenta.movimiento", mov.id)),
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
                "owed": 0.0,
                "owed_note": "",
                "res_model": "distri.route.return",
                "res_id": ret.id,
                "key": (when, _SOURCE_ORDER[SOURCE_RETURN], ret.id),
                "line_key": statement_line_key("distri.route.return", ret.id),
            })

        # a payment registered from a Mercado Pago card links back to it
        move_ids = [r["res_id"] for r in rows if r["res_model"] == "account.move" and r["credit"]]
        if move_ids:
            cards = self.env["distri.mp.collection"].sudo().search(
                [("payment_id.move_id", "in", move_ids)])
            by_move = {c.payment_id.move_id.id: c.id for c in cards}
            for row in rows:
                if row["res_model"] == "account.move" and row["res_id"] in by_move:
                    row["card_id"] = by_move[row["res_id"]]
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
        balance = rows[-1]["balance"] if rows else 0.0
        # staff only (the customer page never reads them): shown, never applied
        candidates = [{
            "date": card.date_day,
            "name": card.name,
            "amount": card.amount,
            "note": card.candidate_note or "",
            "has_receipt": bool(card.receipt_id),
            "res_id": card.id,
        } for card in self._distri_candidate_cards()]
        candidate_amount = sum(c["amount"] for c in candidates)
        return {
            "candidates": candidates,
            "candidate_amount": candidate_amount,
            "whatif_balance": balance - candidate_amount,
            "rows": selected,
            "opening": opening,
            "closing": closing,
            "balance": balance,
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
        candidates = data.get("candidates") or []
        if not rows and not candidates:
            return "<p class='text-muted'>Sin movimientos.</p>"
        out = [
            "<div class='o_distri_estado'>",
            "<p><b>Saldo%s: %s</b>%s</p>" % (
                " actual" if candidates else "",
                html_escape(_money(data["balance"])),
                (" · incluye devoluciones informadas a controlar por %s"
                 % html_escape(_money(data["provisional"]))) if data["provisional"] else ""),
        ]
        owed = [r for r in rows if r.get("owed")]
        if owed:
            out.append("<p class='text-danger o_distri_estado_adeudadas'><b>Facturas adeudadas "
                       "(revisadas): %d por %s</b> · en rojo en la tabla</p>" % (
                           len(owed), html_escape(_money(sum(r["owed"] for r in owed)))))
        if candidates:
            out.append("<p><b>%s: %s</b> · %d %s por %s, no restados del saldo</p>" % (
                WHATIF_LABEL, html_escape(_money(data["whatif_balance"])), len(candidates),
                "pago a confirmar" if len(candidates) == 1 else "pagos a confirmar",
                html_escape(_money(data["candidate_amount"]))))
        out += [
            "<table class='table table-sm table-striped o_distri_estado_tabla'>",
            "<thead><tr><th>Fecha</th><th>Tipo</th><th>Comprobante</th><th>Remito</th>"
            "<th>Concepto</th><th>Origen</th><th class='text-end'>Debe</th>"
            "<th class='text-end'>Haber</th><th class='text-end'>Saldo</th></tr></thead><tbody>",
        ]
        # the candidates first, muted: the «Haber» in parentheses is not applied
        for cand in candidates:
            out.append(
                "<tr class='table-secondary text-muted o_distri_estado_candidato'><td>%s</td>"
                "<td>%s <span class='badge text-bg-secondary'>%s</span></td><td>%s</td><td></td>"
                "<td>%s</td><td>Mercado Pago</td><td></td><td class='text-end'>(%s)</td>"
                "<td class='text-end'>no resta</td></tr>" % (
                    cand["date"].strftime("%d/%m/%Y") if cand["date"] else "",
                    html_escape(CANDIDATE_ROW_LABEL),
                    "llegó el comprobante" if cand["has_receipt"] else "sin comprobante",
                    html_escape(cand["name"]), html_escape(cand["note"]),
                    html_escape(_money(cand["amount"]))))
        for row in rows:
            out.append(
                "<tr%s><td>%s</td><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                "<td class='text-end'>%s</td><td class='text-end'>%s</td>"
                "<td class='text-end'>%s</td></tr>" % (
                    " class='table-warning'" if row["provisional"] else (
                        " class='table-danger o_distri_estado_adeudada'%s" % (
                            " title='%s'" % html_escape(row["owed_note"])
                            if row.get("owed_note") else "") if row.get("owed") else ""),
                    row["date"].strftime("%d/%m/%Y") if row["date"] else "",
                    html_escape(row["doc_type"]),
                    " <span class='badge text-bg-warning'>a controlar</span>"
                    if row["provisional"] else (
                        " <span class='badge text-bg-danger'>debe %s</span>" % html_escape(
                            _money(row["owed"])) if row.get("owed") else ""),
                    _statement_number(row), html_escape(row["remito"]),
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
    balance = fields.Monetary(string="Saldo actual", compute="_compute_statement")
    candidate_amount = fields.Monetary(string="Pagos a confirmar", compute="_compute_statement",
                                       help="Candidatos sin comprobante: no restan del saldo.")
    whatif_balance = fields.Monetary(string=WHATIF_LABEL, compute="_compute_statement")
    # «Candidato · falta el comprobante» (19.0.1.45.0): notes and «Aprobar»
    # from the statement, administration only (the board's access)
    candidate_ids = fields.Many2many(
        "distri.mp.collection", "distri_estado_cuenta_candidate_rel", "wizard_id",
        "collection_id", string=CANDIDATE_ROW_LABEL, compute="_compute_candidate_ids",
        store=True, readonly=False)
    statement_html = fields.Html(string="Movimientos", compute="_compute_statement",
                                 sanitize=False)

    def _check_access_saldos(self):
        user = self.env.user
        if not (self.env.su or any(user.has_group(g) for g in VE_SALDOS.split(","))):
            raise UserError("Solo vendedores, administración y gerencia ven el estado de cuenta.")

    def _check_partner_visible(self):
        """19.0.1.96.0 (audit F9): the statement is computed with sudo, so
        the wizard itself checks that the user may see the customer: the
        same record rules as the contacts (a seller, his portfolio). The
        customer is the commercial partner; seeing one of its contacts is
        enough (the tag may sit on the contact)."""
        if self.env.su:
            return
        Partner = self.env["res.partner"]
        for wizard in self:
            partner = wizard.partner_id
            if partner and not Partner.search_count([
                    "|", ("id", "=", partner.id),
                    ("commercial_partner_id", "=", partner.id)], limit=1):
                raise AccessError("Ese cliente no está en tu cartera: no podés ver su estado "
                                  "de cuenta.")

    @api.model_create_multi
    def create(self, vals_list):
        self._check_access_saldos()
        wizards = super().create(vals_list)
        wizards._check_partner_visible()
        return wizards

    @api.depends("partner_id", "date_from", "date_to")
    def _compute_statement(self):
        for wizard in self:
            if not wizard.partner_id:
                wizard.update({"opening": 0.0, "closing": 0.0, "balance": 0.0,
                               "candidate_amount": 0.0, "whatif_balance": 0.0,
                               "statement_html": False})
                continue
            data = wizard._statement()
            wizard.opening = data["opening"]
            wizard.closing = data["closing"]
            wizard.balance = data["balance"]
            wizard.candidate_amount = data["candidate_amount"]
            wizard.whatif_balance = data["whatif_balance"]
            wizard.statement_html = wizard.partner_id.sudo()._distri_statement_table(
                data, newest_first=False)

    @api.depends("partner_id")
    def _compute_candidate_ids(self):
        admin = self.env.su or self.env.user.has_group("distri_pedido.grupo_administracion")
        for wizard in self:
            wizard.candidate_ids = wizard.partner_id._distri_candidate_cards().with_env(
                self.env) if admin and wizard.partner_id else False

    def _statement(self):
        self.ensure_one()
        self._check_access_saldos()
        self._check_partner_visible()
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
