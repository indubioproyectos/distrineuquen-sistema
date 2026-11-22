"""«Cobrar» on the customer invoice, and the cashier without «Pagar» nor
«Banco» (19.0.1.128.0).

- «Cobrar» (account.move `action_distri_cobrar`): the wizard
  `distri.cobrar.wizard` collects in one step with the engine of
  models/cobrar.py: cash, one or more transfers (each with its receipt; the
  file is read with the time cap of models/ocr_comprobante.py and prefills
  the amount) and the destination «A cuenta» / «Factura del sistema» /
  «Factura de Online». A question of the engine (route, debt, repeat, the
  v126 warnings) or a refusal comes back in the wizard itself, which keeps
  the receipts already stored, so nothing is attached twice.
- A cashier loses Odoo's «Pagar» (register payment) and the «Banco»
  journals (owner's decision 8 of 21/11/2026, audit 2026-11-22): transfers are
  collected only with their receipt, through «Cobrar». Gerencia and Sistemas
  keep them. Administración keeps them too unless the user is also Cajero
  (`native_pay_restricted`): the cashiers of the company are Administración
  as well, and the audit's point is that the person who keeps the cash does
  not load bank payments without a receipt. The parameter
  PARAM_ADMIN_CASHIER_KEEPS_BANK = 1 gives them back to Administración +
  Cajero. Checked on the user's own actions (not in sudo: the module's
  flows that already passed their gate, like the v126 crediting in «Mercado
  Pago», which is a bank journal, keep working).
- 19.0.1.129.1: the wizard opens with one empty transfer row (ready for the
  receipt) and a clear «+ Agregar transferencia» button; an empty row (no
  file, no amount) is ignored on collect. Each transfer opens a form with
  its receipt big (views/cobrar.xml, static/src/js/receipt_preview.js).
"""
import base64

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError

from .caja import GRUPO_GERENCIA, is_cashier
from .caja_pago import NO_OPEN_CAJA_NOTICE
from .cobrar import (
    COLLECT_DENIED, CREDIT_OK, DESTINATIONS, QUESTIONS, can_collect, question_field,
)
from .reparto import _money

PARAM_ADMIN_CASHIER_KEEPS_BANK = "distri_pedido.cajero_admin_conserva_banco"
NATIVE_PAY_DENIED = (
    "Los cajeros cobran con «Cobrar»: el efectivo entra a la caja y cada transferencia lleva su "
    "comprobante. «Pagar» y el diario Banco quedan para administración, gerencia y Sistemas.")
BANK_DENIED = ("Los cajeros no cargan pagos en un diario de banco: las transferencias se cobran "
               "con su comprobante, desde «Cobrar».")
QUESTION_BUTTONS = {
    "ruta_ok": "No le pagó al repartidor: cobrar",
    "forzar": "Cobrar igual",
    "deuda_ok": "Sí, cobrar igual",
    "confirmado": "Sí, es otro cobro",
    CREDIT_OK: "Revisé las advertencias: cobrar",
}


def native_pay_restricted(user):
    """The user loses «Pagar» and the «Banco» journals (module docstring)."""
    if not user or user.share or user._is_superuser() or not is_cashier(user):
        return False
    if user.has_group(GRUPO_GERENCIA) or user.has_group("base.group_system"):
        return False
    if user.has_group("distri_pedido.grupo_administracion"):
        keeps = user.env["ir.config_parameter"].sudo().get_param(PARAM_ADMIN_CASHIER_KEEPS_BANK)
        if str(keeps or "").strip().lower() in ("1", "true", "si", "sí"):
            return False
    return True


def _check_native_pay(env):
    if not env.su and native_pay_restricted(env.user):
        raise AccessError(NATIVE_PAY_DENIED)


def _amount_text(value):
    return ("%.2f" % value).replace(".", ",") if value and value > 0 else ""


class AccountMoveCobrar(models.Model):
    _inherit = "account.move"

    distri_can_cobrar = fields.Boolean(compute="_compute_distri_pay_buttons")
    distri_native_pay_allowed = fields.Boolean(compute="_compute_distri_pay_buttons")

    @api.depends_context("uid")
    def _compute_distri_pay_buttons(self):
        user = self.env.user
        collect, native = can_collect(user), not native_pay_restricted(user)
        for move in self:
            move.distri_can_cobrar = collect and move.move_type == "out_invoice"
            move.distri_native_pay_allowed = native

    def action_distri_cobrar(self):
        """«Cobrar»: the one-step wizard on this invoice."""
        self.ensure_one()
        if not can_collect(self.env.user):
            raise AccessError(COLLECT_DENIED)
        move = self.sudo()
        if move.move_type != "out_invoice" or move.state != "posted":
            raise UserError("Solo se cobra una factura de cliente validada.")
        open_invoice = move.amount_residual > 0.004
        wizard = self.env["distri.cobrar.wizard"].create({
            "partner_id": move.commercial_partner_id.id,
            "destination": "lab" if open_invoice else "account",
            "invoice_id": move.id if open_invoice else False,
        })
        return wizard._reopen()

    def action_register_payment(self):
        _check_native_pay(self.env)
        return super().action_register_payment()

    def action_force_register_payment(self):
        _check_native_pay(self.env)
        return super().action_force_register_payment()


class AccountMoveLineCobrar(models.Model):
    _inherit = "account.move.line"

    def action_register_payment(self, ctx=None):
        _check_native_pay(self.env)
        return super().action_register_payment(ctx=ctx)


class AccountPaymentRegisterNoBank(models.TransientModel):
    _inherit = "account.payment.register"

    @api.model
    def default_get(self, fields_list):
        _check_native_pay(self.env)
        return super().default_get(fields_list)

    def _get_batch_available_journals(self, batch_result):
        journals = super()._get_batch_available_journals(batch_result)
        if not self.env.su and native_pay_restricted(self.env.user):
            journals = journals.filtered(lambda j: j.type != "bank")
        return journals


class AccountPaymentNoBank(models.Model):
    _inherit = "account.payment"

    def _distri_check_bank(self):
        if self.env.su or not native_pay_restricted(self.env.user):
            return
        if self.filtered(lambda p: p.payment_type == "inbound" and p.partner_type == "customer"
                         and p.journal_id.type == "bank"):
            raise AccessError(BANK_DENIED)

    @api.model_create_multi
    def create(self, vals_list):
        payments = super().create(vals_list)
        payments._distri_check_bank()
        return payments

    def write(self, vals):
        result = super().write(vals)
        if {"journal_id", "payment_type", "partner_type"} & set(vals):
            self._distri_check_bank()
        return result


class DistriCobrarWizard(models.TransientModel):
    _name = "distri.cobrar.wizard"
    _description = "Cobrar"

    partner_id = fields.Many2one("res.partner", string="Cliente", required=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True,
                                 default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    destination = fields.Selection(DESTINATIONS, string="Aplicar a", required=True,
                                   default="account")
    invoice_id = fields.Many2one("account.move", string="Factura del sistema",
                                 domain="[('id', 'in', invoice_choice_ids)]")
    invoice_choice_ids = fields.Many2many("account.move", compute="_compute_choices",
                                          compute_sudo=True)
    history_id = fields.Many2one("distri.cuenta.movimiento", string="Factura de Online",
                                 domain="[('id', 'in', history_choice_ids)]")
    history_choice_ids = fields.Many2many("distri.cuenta.movimiento", compute="_compute_choices",
                                          compute_sudo=True)
    balance = fields.Monetary(string="Saldo de la cuenta", compute="_compute_choices",
                              compute_sudo=True)
    owed = fields.Monetary(string="Debe la factura", compute="_compute_owed")
    cash_amount = fields.Monetary(string="Efectivo")
    line_ids = fields.One2many("distri.cobrar.wizard.line", "wizard_id", string="Transferencias",
                               default=lambda self: [Command.create({})])
    transfer_total = fields.Monetary(string="Total transferencias", compute="_compute_total")
    total = fields.Monetary(string="Total a cobrar", compute="_compute_total")
    note = fields.Char(string="Nota", size=200)
    caja_hint = fields.Char(compute="_compute_caja_hint")
    question = fields.Text(readonly=True)
    question_field = fields.Char(readonly=True)
    question_button = fields.Char(compute="_compute_question_button")
    confirm = fields.Boolean(string="Sí, lo revisé")
    answered = fields.Char(readonly=True)
    error_text = fields.Text(readonly=True)

    @api.depends("partner_id")
    def _compute_choices(self):
        Move = self.env["account.move"].sudo()
        for wizard in self:
            partner = wizard.partner_id.commercial_partner_id.sudo()
            if not partner:
                wizard.update({"invoice_choice_ids": False, "history_choice_ids": False,
                               "balance": 0.0})
                continue
            data = partner._distri_statement()
            history = [row["res_id"] for row in data["rows"]
                       if row.get("owed") and row["res_model"] == "distri.cuenta.movimiento"]
            moves = Move.search([("move_type", "=", "out_invoice"), ("state", "=", "posted"),
                                 ("commercial_partner_id", "=", partner.id),
                                 ("amount_residual", ">", 0.004)])
            wizard.invoice_choice_ids = [(6, 0, moves.ids)]
            wizard.history_choice_ids = [(6, 0, history)]
            wizard.balance = data["balance"]

    @api.depends("destination", "invoice_id", "history_id")
    def _compute_owed(self):
        for wizard in self:
            owed = 0.0
            if wizard.destination == "lab" and wizard.invoice_id:
                owed = wizard.invoice_id.sudo().amount_residual
            elif wizard.destination == "online" and wizard.history_id:
                row = next((r for r in wizard.partner_id.sudo()._distri_statement_rows()
                            if r.get("history_id") == wizard.history_id.id), None)
                owed = row["owed"] if row else 0.0
            wizard.owed = owed

    @api.depends("cash_amount", "line_ids.amount")
    def _compute_total(self):
        for wizard in self:
            wizard.transfer_total = sum(wizard.line_ids.mapped("amount"))
            wizard.total = wizard.cash_amount + wizard.transfer_total

    @api.depends("cash_amount")
    @api.depends_context("uid")
    def _compute_caja_hint(self):
        user = self.env.user
        waiting = is_cashier(user) and not self.env["distri.caja"].current_for(user)
        for wizard in self:
            wizard.caja_hint = NO_OPEN_CAJA_NOTICE if waiting and wizard.cash_amount else False

    @api.depends("question_field")
    def _compute_question_button(self):
        for wizard in self:
            wizard.question_button = QUESTION_BUTTONS.get(wizard.question_field or "", False)

    @api.onchange("destination")
    def _onchange_destination(self):
        if self.destination != "lab":
            self.invoice_id = False
        if self.destination != "online":
            self.history_id = False

    @api.onchange("cash_amount", "line_ids", "destination", "invoice_id", "history_id")
    def _onchange_reset_question(self):
        # another amount or destination: the question asked is no longer the one to confirm
        self.confirm = False

    def _reopen(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Cobrar",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "views": [(self.env.ref("distri_pedido.view_cobrar_wizard_form").id, "form")],
            "target": "new",
        }

    def action_collect(self):
        """«Cobrar»: stores the receipts, then collects (models/cobrar.py)."""
        self.ensure_one()
        Cobro = self.env["distri.cobro"]
        flags = set(filter(None, (self.answered or "").split(",")))
        if self.question_field and self.confirm:
            flags.add(self.question_field)
        transfers = []
        for line in self.line_ids:
            if line._is_empty():
                continue  # the row the wizard opens with, left unused
            transfers.append({
                "line": line.id,
                "amount": _amount_text(line.amount),
                "receipt_id": line.receipt_id.id or False,
                "file": None if line.receipt_id or not line.receipt_file else (
                    line.receipt_filename or "comprobante",
                    base64.b64decode(line.receipt_file)),
            })
        try:
            stored = Cobro.collect_receipts(self.partner_id.id, transfers)
        except UserError as exc:
            return self._show_error(exc, flags)
        for transfer in stored:
            self.line_ids.filtered(lambda l: l.id == transfer["line"]).receipt_id = \
                transfer["receipt"].id
        try:
            with self.env.cr.savepoint():
                cobro = Cobro.collect(
                    self.partner_id.id, self.destination,
                    invoice_id=self.invoice_id.id if self.destination == "lab" else False,
                    history_id=self.history_id.id if self.destination == "online" else False,
                    cash_amount=_amount_text(self.cash_amount), transfers=stored, note=self.note,
                    confirmed="confirmado" in flags, route_ok="ruta_ok" in flags,
                    force="forzar" in flags, over_ok="deuda_ok" in flags,
                    credit_ok=CREDIT_OK in flags)
        except QUESTIONS as exc:
            self.write({"question": exc.args[0], "question_field": question_field(exc),
                        "confirm": False, "answered": ",".join(sorted(flags)),
                        "error_text": False})
            return self._reopen()
        except UserError as exc:
            return self._show_error(exc, flags)
        message = "Cobrado: %s." % cobro.memo
        if cobro.caja_notice:
            message = "%s %s" % (message, cobro.caja_notice)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "Cobrar · %s" % _money(cobro.amount), "message": message,
                       "type": "warning" if cobro.caja_notice else "success",
                       "sticky": bool(cobro.caja_notice),
                       "next": {"type": "ir.actions.act_window_close"}},
        }

    def _show_error(self, exc, flags):
        self.write({"error_text": exc.args[0] if exc.args else str(exc), "question": False,
                    "question_field": False, "confirm": False,
                    "answered": ",".join(sorted(flags))})
        return self._reopen()


class DistriCobrarWizardLine(models.TransientModel):
    _name = "distri.cobrar.wizard.line"
    _description = "Cobrar: transferencia"

    wizard_id = fields.Many2one("distri.cobrar.wizard", required=True, ondelete="cascade")
    currency_id = fields.Many2one(related="wizard_id.currency_id")
    receipt_file = fields.Binary(string="Comprobante", attachment=False)
    receipt_filename = fields.Char(string="Archivo")
    amount = fields.Monetary(string="Importe")
    receipt_id = fields.Many2one("distri.whatsapp.message", string="Comprobante subido",
                                 readonly=True)
    read_note = fields.Char(string="Lectura", readonly=True)
    receipt_label = fields.Char(string="Comprobante", compute="_compute_receipt_label")

    @api.depends("receipt_file", "receipt_filename", "receipt_id")
    def _compute_receipt_label(self):
        for line in self:
            if line.receipt_file or line.receipt_id:
                line.receipt_label = line.receipt_filename or "Comprobante cargado"
            else:
                line.receipt_label = "Sin archivo: toque aquí para cargar la foto o el PDF"

    def _is_empty(self):
        """No file, no stored receipt and no amount: nothing to collect."""
        self.ensure_one()
        return not (self.receipt_file or self.receipt_id or self.amount > 0)

    @api.onchange("receipt_file")
    def _onchange_receipt_file(self):
        """A new file: read with the time cap; the amount read fills an empty
        amount (the person checks it)."""
        self.receipt_id = False
        self.read_note = False
        if not self.receipt_file:
            return
        try:
            raw = base64.b64decode(self.receipt_file)
        except (TypeError, ValueError):
            return
        preview = self.env["distri.whatsapp.message"]._distri_upload_preview(raw)
        if preview.get("amount"):
            if not self.amount:
                self.amount = preview["amount"]
            self.read_note = "Leído: %s%s" % (
                _money(preview["amount"]), "" if preview.get("amount_sure") else " (dudoso)")
        else:
            self.read_note = preview.get("notice") or "No se leyó el importe: escribilo."
