"""«Pago a proveedor de Distri» (19.0.1.129.0, decision 7 of 21/11/2026).

A customer transfers straight to one of the company's suppliers, with
Gerencia's authorization. The customer is credited in a journal of its own,
«Pagos a proveedores por cuenta de clientes» (code PPCC), never in «Mercado
Pago»: it is not matched against the Mercado Pago report (the audit and the
bank-twin guard leave that journal out) and it is listed apart, «Autorizados
por Gerencia», for the accountant.

Only the group «Cobros: pago a proveedor de Distri» loads it: Gerencia (the
group is implied by grupo_gerencia), the administrator (Denis) and the
technical admin (Ale), granted by migrations/19.0.1.129.0. Kept: the
receipt, the supplier, the amount, who loaded it and who authorized it (a
Gerencia or Sistemas user).
"""
import logging

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .reparto import _money
from .reparto_cliente import today_ar

_logger = logging.getLogger(__name__)

SUPPLIER_GROUP = "distri_pedido.grupo_pago_proveedor"
SUPPLIER_JOURNAL_CODE = "PPCC"
SUPPLIER_JOURNAL_NAME = "Pagos a proveedores por cuenta de clientes"
SUPPLIER_DENIED = ("Solo Gerencia, el administrador y el admin técnico cargan un pago a proveedor "
                   "por cuenta de un cliente (grupo «Cobros: pago a proveedor de Distri»).")
AUTHORIZER_GROUPS = ("distri_pedido.grupo_gerencia", "base.group_system")


def can_supplier_pay(env):
    return bool(env.su or env.user.has_group(SUPPLIER_GROUP))


def _check_supplier(env):
    if not can_supplier_pay(env):
        raise AccessError(SUPPLIER_DENIED)


class AccountJournalSupplier(models.Model):
    _inherit = "account.journal"

    @api.model
    def _distri_supplier_journal(self, company=None, create=False):
        """«Pagos a proveedores por cuenta de clientes» of the company (an
        empty recordset when missing and `create` is False)."""
        company = company or self.env.company
        Journal = self.sudo()
        journal = Journal.search([("code", "=", SUPPLIER_JOURNAL_CODE),
                                  ("company_id", "=", company.id)], limit=1)
        if not journal and create:
            journal = Journal.create({"name": SUPPLIER_JOURNAL_NAME, "code": SUPPLIER_JOURNAL_CODE,
                                      "type": "bank", "company_id": company.id})
            # the money never enters an account of the company: the payment
            # is settled at once, like «Mercado Pago»
            journal.inbound_payment_method_line_ids.write(
                {"payment_account_id": journal.default_account_id.id})
        return journal

    @api.model
    def _distri_ensure_supplier_journal(self):
        """On module update, in the companies with accounting set up."""
        for company in self.env["res.company"].sudo().search([]):
            if self.sudo().search_count([("company_id", "=", company.id), ("type", "=", "bank")]):
                self._distri_supplier_journal(company, create=True)


class DistriSupplierPayment(models.Model):
    _name = "distri.supplier.payment"
    _description = "Pago a proveedor por cuenta de un cliente"
    _inherit = ["mail.thread"]
    _order = "date desc, id desc"

    name = fields.Char(string="Número", readonly=True, copy=False, default="Nuevo")
    company_id = fields.Many2one("res.company", required=True, readonly=True,
                                 default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    partner_id = fields.Many2one("res.partner", string="Cliente", required=True, tracking=True,
                                 domain="[('distri_generic_partner', '=', False), "
                                        "('parent_id', '=', False)]")
    supplier_id = fields.Many2one("res.partner", string="Proveedor", required=True,
                                  tracking=True)
    amount = fields.Monetary(string="Importe", required=True, tracking=True)
    date = fields.Date(string="Fecha de la transferencia", required=True,
                       default=lambda self: today_ar())
    receipt_file = fields.Binary(string="Comprobante", attachment=True, required=True)
    receipt_filename = fields.Char(string="Archivo")
    note = fields.Char(string="Nota")
    loaded_by_id = fields.Many2one("res.users", string="Cargó", readonly=True,
                                   default=lambda self: self.env.user)
    authorized_by_id = fields.Many2one(
        "res.users", string="Autorizó", required=True, tracking=True,
        domain="[('share', '=', False)]",
        help="Quién de Gerencia autorizó que el cliente le pague directo al proveedor.")
    state = fields.Selection([("draft", "Borrador"), ("posted", "Acreditado"),
                              ("cancelled", "Anulado")], string="Estado", default="draft",
                             required=True, readonly=True, tracking=True)
    payment_id = fields.Many2one("account.payment", string="Pago", readonly=True, copy=False)
    posted_at = fields.Datetime(string="Acreditado el", readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        _check_supplier(self.env)
        for vals in vals_list:
            vals["loaded_by_id"] = self.env.uid
        records = super().create(vals_list)
        for rec in records:
            super(DistriSupplierPayment, rec).write({"name": "PPC-%05d" % rec.id})
        return records

    def write(self, vals):
        _check_supplier(self.env)
        if self.filtered(lambda r: r.state != "draft") and set(vals) - {
                "state", "payment_id", "posted_at", "note"}:
            raise UserError("Un pago a proveedor ya acreditado no se cambia: anulalo y cargalo de "
                            "nuevo.")
        return super().write(vals)

    def action_post(self):
        """Credits the customer in «Pagos a proveedores por cuenta de
        clientes», oldest invoice first."""
        _check_supplier(self.env)
        for rec in self:
            if rec.state != "draft":
                raise UserError("%s ya está acreditado o anulado." % rec.name)
            if rec.amount <= 0:
                raise UserError("Escribí el importe.")
            if not rec.receipt_file:
                raise UserError("Adjuntá el comprobante de la transferencia al proveedor.")
            if rec.partner_id.distri_generic_partner:
                raise UserError("Elegí un cliente con cuenta corriente.")
            authorizer = rec.authorized_by_id
            if not any(authorizer.has_group(g) for g in AUTHORIZER_GROUPS):
                raise UserError("Lo autoriza un usuario de Gerencia: %s no lo es." % (
                    authorizer.name or ""))
            rec._post_one()
        return True

    def _post_one(self):
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id.sudo()
        journal = self.env["account.journal"]._distri_supplier_journal(self.company_id,
                                                                         create=True)
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": partner.id,
            "amount": self.amount,
            "journal_id": journal.id,
            "date": self.date,
            "memo": "Pago a proveedor %s por cuenta del cliente · autorizó %s · %s" % (
                self.supplier_id.name, self.authorized_by_id.name, self.name),
            "company_id": self.company_id.id,
        })
        payment.action_post()
        self.env["distri.route"].sudo()._apply_oldest_first(payment, partner)
        self.env["ir.attachment"].sudo().create({
            "name": self.receipt_filename or "comprobante-%s" % self.name,
            "datas": self.receipt_file, "res_model": "account.payment", "res_id": payment.id})
        self.sudo().write({"state": "posted", "payment_id": payment.id,
                           "posted_at": fields.Datetime.now()})
        text = "Pago a proveedor %s: %s del cliente, cargado por %s y autorizado por %s (pago %s)." \
            % (self.supplier_id.name, _money(self.amount), self.loaded_by_id.name,
               self.authorized_by_id.name, payment.name)
        self.message_post(body=text)
        partner.message_post(body=Markup("<b>%s</b>") % text)
        _logger.info("Supplier payment %s posted by %s: payment %s", self.id,
                     self.env.user.login, payment.id)
        return payment

    def action_cancel(self):
        _check_supplier(self.env)
        for rec in self.filtered(lambda r: r.state == "posted"):
            payment = rec.sudo().payment_id
            if payment and payment.state not in ("draft", "canceled"):
                # 19.0.1.129.24: cancelled, not deleted (keeps its number)
                payment._distri_cancel_keep_number()
            rec.write({"state": "cancelled"})
            rec.message_post(body="Anulado por %s: pago %s anulado." % (
                self.env.user.name, payment.name or ""))
        self.filtered(lambda r: r.state == "draft").write({"state": "cancelled"})
        return True

    def action_view_payment(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "account.payment",
                "view_mode": "form", "res_id": self.payment_id.id}
