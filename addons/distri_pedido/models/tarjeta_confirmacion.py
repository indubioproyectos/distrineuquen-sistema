"""Confirmation card of the migration board (19.0.1.63.0).

The card of an electronic payment, opened from the migration board
(views/tarjeta_confirmacion.xml, a primary view of the collections form),
shows everything at once: one compact line with the customer, who proposed
it, the reason and the column, then three columns that scroll on their own:

- the balance of the proposed customer (`card_balance_html`): the same
  statement as «Estado de cuenta» (res.partner._distri_statement: the Online
  history, the lab documents and the reported returns), newest first, with
  the current balance and the balance once this card is approved;
- the receipts chat centered on the card's photo (static/src/js/card_chat.js,
  the «Chat de comprobantes» viewer embedded);
- the Mercado Pago report next to what was read on the photo
  (`card_mp_html`), in the order a transfer receipt prints it.

Which invoice the payment pays. By default a payment is «a cuenta»: nothing
is cited and, as always, it is applied to the oldest open lab invoices. The
administrator may choose one invoice of the customer: an invoice of the
Online history (`pay_invoice_id`) or an open invoice of this Odoo
(`pay_move_id`). The payment's memo cites its number («· 0001/2026/07369»,
the way the cash sheets cite theirs) and a lab invoice (also one with the
history invoice's number) is reconciled first. A delivery authorized by hand
hands its choice to the income of the report that confirms it.
"""
from datetime import timezone

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError

from .cobros import AMOUNT_TOLERANCE, _check_cobros, digits, format_cuit, operation_close
from .estado_cuenta import _money
from .migracion import names_match
from .reparto_cliente import AR_TZ, friendly_case

# the newest movements of the statement shown on the card; «Estado de
# cuenta» has the whole history
BALANCE_ROWS = 200
ON_ACCOUNT_LABEL = "A cuenta"
CHOOSABLE_STATES = ("draft", "authorized")
PAY_FIELDS = ("pay_invoice_id", "pay_move_id")


def _when(moment):
    """Naive UTC datetime -> «31/10/2026 10:40» in Argentina."""
    if not moment:
        return ""
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ).strftime("%d/%m/%Y %H:%M")


def _day(value):
    return value.strftime("%d/%m/%Y") if value else ""


class CuentaMovimientoInvoiceLabel(models.Model):
    _inherit = "distri.cuenta.movimiento"

    @api.depends_context("distri_invoice_label")
    @api.depends("comprobante", "fecha", "debe")
    def _compute_display_name(self):
        """With `distri_invoice_label` (the card's invoice choice), the number
        with its date and amount: «0001/2026/07369 · 31/10/2026 · $ 1.570.300»."""
        if not self.env.context.get("distri_invoice_label"):
            return super()._compute_display_name()
        for mov in self:
            mov.display_name = " · ".join(p for p in (
                mov.comprobante or "", _day(mov.fecha), _money(mov.debe)) if p)


class DistriMpCollectionCard(models.Model):
    _inherit = "distri.mp.collection"

    pay_invoice_id = fields.Many2one(
        "distri.cuenta.movimiento", string="Paga la factura", ondelete="set null", copy=False,
        tracking=True, index="btree_not_null",
        help="Factura de la historia de Odoo Online que paga esta transferencia. El pago la cita "
             "en el memo. Vacío: a cuenta.")
    pay_move_id = fields.Many2one(
        "account.move", string="Paga la factura del lab", ondelete="set null", copy=False,
        tracking=True, index="btree_not_null",
        help="Factura abierta de este Odoo que paga esta transferencia: el pago la cita en el "
             "memo y se concilia primero con ella. Vacío: a cuenta.")
    pay_partner_id = fields.Many2one("res.partner", compute="_compute_pay_partner")
    pay_has_lab_invoices = fields.Boolean(compute="_compute_pay_partner")
    pay_invoice_label = fields.Char(string="Paga", compute="_compute_pay_invoice_label")
    card_balance_html = fields.Html(string="Saldo del cliente", sanitize=False,
                                    compute="_compute_card_balance_html")
    card_mp_html = fields.Html(string="Reporte de Mercado Pago", sanitize=False,
                               compute="_compute_card_mp_html")

    _pay_one_invoice = models.Constraint(
        "CHECK(pay_invoice_id IS NULL OR pay_move_id IS NULL)",
        "Elegí una sola factura: de la historia de Online o del lab.")

    # ------------------------------------------------------------ invoice choice

    @api.depends("partner_id")
    def _compute_pay_partner(self):
        Move = self.env["account.move"].sudo()
        for card in self:
            partner = card.partner_id.commercial_partner_id
            card.pay_partner_id = partner
            card.pay_has_lab_invoices = bool(partner) and bool(Move.search_count(
                card._lab_invoice_domain(partner), limit=1))

    @api.model
    def _lab_invoice_domain(self, partner):
        return [("move_type", "=", "out_invoice"), ("state", "=", "posted"),
                ("payment_state", "in", ("not_paid", "partial")),
                ("partner_id", "child_of", partner.id)]

    @api.depends("pay_invoice_id", "pay_move_id", "kind", "state", "income_id")
    def _compute_pay_invoice_label(self):
        for card in self:
            number, when, amount = card._chosen_invoice_data()
            if not number:
                card.pay_invoice_label = ON_ACCOUNT_LABEL
            else:
                card.pay_invoice_label = "Factura %s" % " · ".join(
                    p for p in (number, _day(when), _money(amount) if amount else "") if p)

    def _chosen_source(self):
        """The card whose choice counts: an income confirmed from a delivery
        authorized by hand takes the authorization's when it has none."""
        self.ensure_one()
        if not (self.pay_invoice_id or self.pay_move_id) and self.kind == "income":
            card = self._authorized_card()
            if card and (card.pay_invoice_id or card.pay_move_id):
                return card
        return self

    def _chosen_invoice_data(self):
        """(number, date, amount) of the chosen invoice, or ("", False, 0)."""
        self.ensure_one()
        card = self._chosen_source().sudo()
        if card.pay_move_id:
            move = card.pay_move_id
            return move.name or "", move.invoice_date or move.date, move.amount_total
        if card.pay_invoice_id:
            mov = card.pay_invoice_id
            return mov.comprobante or "", mov.fecha, mov.debe
        return "", False, 0.0

    def _chosen_lab_invoice(self, partner):
        """The lab invoice to reconcile first: the one chosen, or the open lab
        invoice with the number of the chosen history invoice."""
        self.ensure_one()
        card = self._chosen_source().sudo()
        if card.pay_move_id:
            return card.pay_move_id
        number = card.pay_invoice_id.comprobante
        if not number:
            return self.env["account.move"]
        return self.env["account.move"].sudo().search(
            self._lab_invoice_domain(partner) + [("name", "=", number)], limit=1)

    def _payment_memo(self):
        """The chosen invoice is cited at the end of the memo, the way the
        cash sheets cite theirs: «Mercado Pago 1234 · Juan Pérez · 0001/2026/07369»."""
        memo = super()._payment_memo()
        number = self._chosen_invoice_data()[0]
        return "%s · %s" % (memo, number) if number else memo

    def _reconcile_chosen_invoice(self, payment):
        """Called by `_confirm_one` before the oldest-first application: the
        payment goes first to the chosen lab invoice; what is left follows the
        usual order."""
        self.ensure_one()
        partner = payment.partner_id.commercial_partner_id
        move = self._chosen_lab_invoice(partner)
        if not move or move.state != "posted":
            return
        counterpart = payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
        lines = move.line_ids.filtered(
            lambda l: l.account_id in counterpart.account_id and not l.reconciled
            and l.amount_residual > 0)
        if counterpart and lines:
            (counterpart + lines).reconcile()

    def write(self, vals):
        if any(name in vals for name in PAY_FIELDS):
            # which invoice the payment goes to is part of authorizing it
            _check_cobros(self.env)
            closed = self.filtered(lambda c: c.state not in CHOOSABLE_STATES)
            if closed:
                raise UserError("La factura que paga se elige antes de confirmar: %s ya no está "
                                "pendiente." % ", ".join(closed.mapped("name")))
            # one choice at a time: choosing one clears the other
            if vals.get("pay_invoice_id") and "pay_move_id" not in vals:
                vals = dict(vals, pay_move_id=False)
            elif vals.get("pay_move_id") and "pay_invoice_id" not in vals:
                vals = dict(vals, pay_invoice_id=False)
        if "partner_id" in vals:
            # another customer: the invoice chosen for the previous one goes
            changed = self.filtered(lambda c: (c.pay_invoice_id or c.pay_move_id)
                                    and c.partner_id.id != vals["partner_id"])
            if changed:
                super(DistriMpCollectionCard, changed).write(
                    {"pay_invoice_id": False, "pay_move_id": False})
        return super().write(vals)

    def action_choose_invoice(self):
        """«Elegir factura»: a dialog with the customer's invoices, newest first."""
        self.ensure_one()
        _check_cobros(self.env)
        if self.state not in CHOOSABLE_STATES or self.kind == "receipt":
            raise UserError("Solo se elige la factura de un cobro pendiente con movimiento o "
                            "comprobante por verificar.")
        if not self.partner_id:
            raise UserError("Elegí el cliente antes de elegir la factura que paga.")
        return {
            "type": "ir.actions.act_window",
            "name": "Factura que paga %s" % self.name,
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "views": [(self.env.ref("distri_pedido.view_mp_collection_pay_invoice").id, "form")],
            "target": "new",
            "context": {"distri_invoice_label": True},
        }

    def action_pay_on_account(self):
        """«A cuenta»: no invoice cited."""
        _check_cobros(self.env)
        self.write({"pay_invoice_id": False, "pay_move_id": False})
        return {"type": "ir.actions.act_window_close"}

    def action_card_statement(self):
        """«Estado de cuenta» of the customer shown in the balance column."""
        self.ensure_one()
        partner = self.partner_id or self.suggested_partner_id
        if not partner:
            raise UserError("La tarjeta no tiene cliente.")
        return partner.commercial_partner_id.action_distri_estado_cuenta()

    # ------------------------------------------------------------ balance column

    def _card_applied(self):
        """The card's money is already in the customer's balance."""
        self.ensure_one()
        return self.state == "confirmed" or (
            self.kind == "verify" and self.income_id.state == "confirmed")

    @api.depends("partner_id", "suggested_partner_id", "amount", "state", "kind", "income_id",
                 "pay_invoice_id", "pay_move_id")
    @api.depends_context("uid")
    def _compute_card_balance_html(self):
        for card in self:
            card.card_balance_html = card._card_balance_html()

    def _card_balance_html(self):
        self.ensure_one()
        partner = self.partner_id or self.suggested_partner_id
        if not partner:
            return Markup("<p class='o_distri_card_empty'>Sin cliente propuesto: elegí el cliente "
                          "para ver su saldo.</p>")
        partner = partner.commercial_partner_id.sudo()
        if partner.distri_generic_partner:
            return Markup("<p class='o_distri_card_empty'>Cliente genérico: no lleva cuenta "
                          "corriente.</p>")
        data = partner._distri_statement()
        balance = data["balance"]
        out = []
        if not self.partner_id:
            out.append(Markup("<p class='o_distri_card_hint'>Cliente sugerido: <b>%s</b>. "
                              "Todavía no está elegido.</p>") % partner.name)
        else:
            out.append(Markup("<p class='o_distri_card_hint'><b>%s</b></p>") % partner.name)
        out.append(Markup("<dl class='o_distri_card_kpis'>"))
        out.append(Markup("<div><dt>Saldo actual</dt><dd>%s</dd></div>") % _money(balance))
        chosen = self._chosen_invoice_data()[0]
        if self._card_applied():
            out.append(Markup("<div><dt>Esta tarjeta</dt><dd class='o_distri_card_small'>ya "
                              "está en el saldo</dd></div>"))
        elif self.state in ("ignored", "apartado"):
            out.append(Markup("<div><dt>Esta tarjeta</dt><dd class='o_distri_card_small'>no "
                              "cambia el saldo</dd></div>"))
        elif self.kind == "receipt":
            out.append(Markup("<div><dt>Esta tarjeta</dt><dd class='o_distri_card_small'>sin "
                              "movimiento en MP: no es un pago</dd></div>"))
        else:
            after = balance - (self.amount or 0.0)
            credit = after < -AMOUNT_TOLERANCE
            out.append(Markup(
                "<div class='%s'><dt>Si se aprueba</dt><dd>%s</dd>"
                "<dd class='o_distri_card_small'>%s − %s%s</dd></div>") % (
                "o_distri_card_kpi_credit" if credit else "o_distri_card_kpi_after",
                _money(after), _money(balance), _money(self.amount),
                " · queda saldo a favor" if credit else ""))
        out.append(Markup("</dl>"))
        if self.kind != "receipt":
            out.append(Markup("<p class='o_distri_card_pays'>Paga: <b>%s</b>%s</p>") % (
                self.pay_invoice_label or ON_ACCOUNT_LABEL,
                "" if chosen else " (se aplica a las facturas más viejas del lab)"))
        if data["candidates"]:
            others = [c for c in data["candidates"] if c["res_id"] != self.id]
            if others:
                out.append(Markup("<p class='o_distri_card_small'>%s pago(s) a confirmar sin "
                                  "comprobante por %s: no restan del saldo.</p>") % (
                    len(others), _money(sum(c["amount"] for c in others))))
        rows = list(reversed(data["rows"]))
        if not rows:
            out.append(Markup("<p class='o_distri_card_empty'>Sin movimientos.</p>"))
            return Markup("").join(out)
        chosen_ids = {
            ("distri.cuenta.movimiento", self._chosen_source().pay_invoice_id.id),
            ("account.move", self._chosen_source().pay_move_id.id)}
        out.append(Markup(
            "<table class='table table-sm o_distri_card_table'><thead><tr><th>Fecha</th>"
            "<th>Comprobante</th><th class='text-end'>Debe</th><th class='text-end'>Haber</th>"
            "<th class='text-end'>Saldo</th></tr></thead><tbody>"))
        for row in rows[:BALANCE_ROWS]:
            is_chosen = (row["res_model"], row["res_id"]) in chosen_ids or (
                chosen and row["number"] == chosen and row["debit"])
            classes = []
            if is_chosen:
                classes.append("o_distri_card_row_chosen")
            elif row["provisional"]:
                classes.append("table-warning")
            out.append(Markup(
                "<tr class='%s'><td>%s</td><td><span class='o_distri_card_doc'>%s</span>%s%s</td>"
                "<td class='text-end'>%s</td><td class='text-end'>%s</td>"
                "<td class='text-end'>%s</td></tr>") % (
                " ".join(classes), _day(row["date"]), row["doc_type"],
                Markup(" %s") % row["number"] if row["number"] else "",
                Markup(" <span class='badge text-bg-primary'>paga esta</span>")
                if is_chosen else "",
                _money(row["debit"]) if row["debit"] else "",
                _money(row["credit"]) if row["credit"] else "",
                _money(row["balance"])))
        out.append(Markup("</tbody></table>"))
        if len(rows) > BALANCE_ROWS:
            out.append(Markup("<p class='o_distri_card_small'>Se muestran los %s movimientos más "
                              "nuevos de %s: el resto, en «Estado de cuenta».</p>") % (
                BALANCE_ROWS, len(rows)))
        return Markup("").join(out)

    # ------------------------------------------------------------ Mercado Pago column

    @api.depends("kind", "income_id", "amount", "date", "source_id", "payer_name", "payer_cuit",
                 "receipt_id", "receipt_amount", "receipt_operation", "receipt_payer_name",
                 "receipt_payer_cuit", "receipt_bank", "operation")
    def _compute_card_mp_html(self):
        for card in self:
            card.card_mp_html = card._card_mp_html()

    def _card_mp_html(self):
        """The report's row next to the photo's reading, in the order of a
        transfer receipt: date and time, amount, from, to, operation, method."""
        self.ensure_one()
        card = self.sudo()
        mp = card if card.kind == "income" else card.income_id
        receipt = card.receipt_id
        out = []
        if not mp:
            out.append(Markup("<p class='o_distri_card_empty'>%s</p>") % (
                "Esperando reporte: el reporte de Mercado Pago todavía no trae esta operación."
                if card.kind == "verify" else
                "Sin movimiento en el reporte de Mercado Pago: la foto no es un pago."))
        if not receipt:
            out.append(Markup("<p class='o_distri_card_empty'>Sin comprobante de WhatsApp: no hay "
                              "foto para comparar.</p>"))

        def same(flag):
            if flag is None:
                return ""
            return Markup(" <span class='o_distri_card_ok'>✓ coincide</span>") if flag else \
                Markup(" <span class='o_distri_card_diff'>≠ distinto</span>")

        def both(a, b):
            return bool(mp) and bool(receipt) and bool(a) and bool(b)

        read_amount = receipt.distri_amount if receipt else 0.0
        read_operation = receipt.distri_operation if receipt else ""
        read_name = receipt.distri_payer_name if receipt else ""
        read_cuit = receipt.distri_payer_cuit if receipt else ""
        mp_cuit = mp.payer_cuit if mp else ""
        own = card.company_id.name or ""
        rows = [
            ("Fecha y hora", _when(mp.date) if mp else "",
             card.receipt_date_label or (_when(receipt.timestamp) + " (enviada)"
                                         if receipt and receipt.timestamp else ""), None),
            ("Importe", _money(mp.amount) if mp else "",
             _money(read_amount) if read_amount else "",
             abs(mp.amount - read_amount) <= AMOUNT_TOLERANCE
             if both(mp and mp.amount, read_amount) else None),
            ("De", friendly_case(mp.payer_name) if mp and mp.payer_name else "",
             read_name or "",
             names_match(read_name, mp.payer_name) if both(mp and mp.payer_name, read_name)
             else None),
            ("CUIT / CUIL", " ".join(p for p in (
                format_cuit(mp_cuit) if mp_cuit else "",
                "(%s)" % mp.arca_name if mp and mp.arca_name else "") if p),
             format_cuit(read_cuit) if read_cuit else "",
             digits(mp_cuit) == digits(read_cuit) if both(mp_cuit, read_cuit) else None),
            ("Banco o billetera de origen", (mp.origin_label or "") if mp else "",
             receipt.distri_bank or "" if receipt else "", None),
            ("Para", ("%s (cuenta de Mercado Pago)" % own) if mp else "",
             (card.receipt_destination or "") + (" · otra cuenta" if card.receipt_other_account
                                                 else ""),
             False if card.receipt_other_account else None),
            ("Operación / N° de transacción", " · ".join(p for p in (
                mp.source_id or "" if mp else "",
                "transferencia %s" % mp.bank_transfer_id if mp and mp.bank_transfer_id else "")
                if p),
             read_operation or (card.operation if card.kind == "verify" else "") or "",
             (operation_close(read_operation, mp.source_id)
              or operation_close(read_operation, mp.bank_transfer_id))
             if both(mp and mp.source_id, read_operation) else None),
            ("Medio", " · ".join(p for p in (
                mp.method_type or "" if mp else "", mp.method or "" if mp else "",
                mp.transaction_type or "" if mp else "") if p), "", None),
        ]
        out.append(Markup(
            "<table class='table table-sm o_distri_card_table o_distri_card_compare'><thead><tr>"
            "<th></th><th>Reporte MP</th><th>Foto (leído)</th></tr></thead><tbody>"))
        for label, report, photo, flag in rows:
            out.append(Markup("<tr><th scope='row'>%s</th><td>%s</td><td>%s%s</td></tr>") % (
                label, report or "—", photo or "—", same(flag)))
        out.append(Markup("</tbody></table>"))
        extra = []
        if mp and mp.fee_amount:
            extra.append(("Comisión", _money(mp.fee_amount)))
        if mp and mp.report_id:
            extra.append(("Reporte", mp.report_id.display_name))
        if card.money_grade:
            extra.append(("Fuerza del vínculo",
                          dict(card._fields["money_grade"].selection)[card.money_grade]))
        if card.evidence_note:
            extra.append(("Evidencia", card.evidence_note))
        if receipt and receipt.distri_amount_confidence:
            extra.append(("Confianza del importe leído", dict(
                receipt._fields["distri_amount_confidence"].selection).get(
                receipt.distri_amount_confidence, "")))
        if extra:
            out.append(Markup("<dl class='o_distri_card_extra'>"))
            for label, value in extra:
                out.append(Markup("<div><dt>%s</dt><dd>%s</dd></div>") % (label, value))
            out.append(Markup("</dl>"))
        return Markup("").join(out)
