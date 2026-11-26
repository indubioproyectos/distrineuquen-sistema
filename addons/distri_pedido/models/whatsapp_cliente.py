"""The customer's WhatsApp: one format, asked at confirmation, and the list
of customers still without it.

Decision of ale (15/11/2026): few customers have a WhatsApp on file. Instead
of blocking the confirmation (or skipping it in silence), every interactive
confirmation of a staff, counter or backend order whose customer has none
asks for it (distri.wa.confirm.wizard), with a skip while the switch
`distri_pedido.pedido_requiere_whatsapp` is off. The number is stored as
+549 + 10 digits (models/pedido_whatsapp.py normalize_whatsapp).
"""
from odoo import api, fields, models, tools
from odoo.exceptions import UserError, ValidationError

from .pedido_whatsapp import (
    ar_mobile, ar_national, format_ar_mobile, normalize_whatsapp,
    only_digits, split_ar_national,
)

LIST_DAYS = 90


class ResPartnerWhatsappFormat(models.Model):
    _inherit = "res.partner"

    @api.model
    def _distri_normalize_whatsapp_vals(self, vals):
        if vals.get("distri_whatsapp"):
            try:
                vals["distri_whatsapp"] = normalize_whatsapp(vals["distri_whatsapp"])
            except ValueError as error:
                raise ValidationError(str(error)) from None
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._distri_normalize_whatsapp_vals(vals)
        return super().create(vals_list)

    def write(self, vals):
        if "distri_whatsapp" in vals:
            vals = self._distri_normalize_whatsapp_vals(dict(vals))
        return super().write(vals)


class DistriWaConfirmWizard(models.TransientModel):
    """«WhatsApp del cliente»: asked when an order is confirmed and its
    customer has no WhatsApp on file."""
    _name = "distri.wa.confirm.wizard"
    _description = "WhatsApp del cliente al confirmar"

    order_id = fields.Many2one("sale.order", string="Pedido", required=True, readonly=True,
                               ondelete="cascade")
    then = fields.Selection([("confirm", "Confirmar"), ("counter", "Venta en mostrador")],
                            default="confirm", required=True, readonly=True)
    customer_name = fields.Char(string="Cliente", compute="_compute_customer")
    generic = fields.Boolean(compute="_compute_customer")
    allow_skip = fields.Boolean(compute="_compute_customer")
    area = fields.Char(string="Código de área",
                       help="Sin el 0. Ej.: 299. Si lo escribís con el 0, se saca solo.")
    number = fields.Char(string="Número",
                         help="Sin el 15. Ej.: 4222444. Si lo escribís con el 15, se saca solo.")
    preview = fields.Char(string="Queda así", compute="_compute_preview")
    error = fields.Char(compute="_compute_preview")

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        order = self.env["sale.order"].browse(values.get("order_id")).exists()
        if order and not (values.get("area") or values.get("number")):
            # a phone already on file is offered as the starting point
            order = order.sudo()
            if order.partner_id.distri_generic_partner:
                candidates = (order.distri_wa_phone, order.distri_telefono)
            else:
                partner = order.partner_id
                candidates = (partner.phone, partner.commercial_partner_id.phone)
            for value in candidates:
                national = ar_national(value)
                if national:
                    values["area"], values["number"] = split_ar_national(national)
                    break
        return values

    @api.depends("order_id")
    def _compute_customer(self):
        allow = not self.env["distri.pedido.whatsapp.settings"].requires_whatsapp()
        for wizard in self:
            order = wizard.order_id.sudo()
            wizard.generic = bool(order.partner_id.distri_generic_partner)
            wizard.customer_name = (order._distri_nombre_web(order.partner_id.name)
                                    if wizard.generic else order.partner_id.name) or ""
            wizard.allow_skip = allow

    @api.depends("area", "number")
    def _compute_preview(self):
        for wizard in self:
            wizard.preview = wizard.error = False
            if not (wizard.area or wizard.number):
                continue
            try:
                canonical = ar_mobile(wizard.area, wizard.number)
            except ValueError as error:
                wizard.error = str(error)
                continue
            area = only_digits(wizard.area).lstrip("0")
            wizard.preview = format_ar_mobile(area, canonical[len("+549") + len(area):])

    @api.onchange("area", "number")
    def _onchange_strip(self):
        """The 0 of the area and the 15 of the number go away as typed."""
        area = only_digits(self.area).lstrip("0")
        number = only_digits(self.number)
        if number.startswith("15") and len(area) + len(number) == 12:
            number = number[2:]
        if self.area and area != self.area:
            self.area = area
        if self.number and number != self.number:
            self.number = number

    # ------------------------------------------------------------ actions

    def _canonical(self):
        self.ensure_one()
        try:
            return ar_mobile(self.area, self.number)
        except ValueError as error:
            raise UserError(str(error)) from None

    def _order_to_confirm(self):
        """The order without the wizard's default_* keys, which would leak
        into the records the confirmation creates."""
        context = {key: value for key, value in self.env.context.items()
                   if not key.startswith("default_")}
        context.update(distri_wa_skip_check=True, distri_wa_ask=False)
        return self.order_id.with_context(context)

    def action_test(self):
        """«Probar en WhatsApp»: the chat opens in another tab and this
        dialog stays open (an act_url with target new and no `close`)."""
        canonical = self._canonical()
        return {"type": "ir.actions.act_url", "url": "https://wa.me/%s" % canonical[1:],
                "target": "new"}

    def action_save_confirm(self):
        self.ensure_one()
        canonical = self._canonical()
        order = self.order_id
        order.check_access("write")
        if order.sudo().partner_id.distri_generic_partner:
            order.sudo().write({"distri_telefono": canonical})
            order.message_post(body="WhatsApp del cliente cargado por %s al confirmar: %s." % (
                self.env.user.name, canonical))
        else:
            partner = order.sudo().partner_id.commercial_partner_id
            partner.write({"distri_whatsapp": canonical})
            partner.message_post(body="WhatsApp cargado por %s al confirmar el pedido %s: %s." % (
                self.env.user.name, order.name, canonical))
        return self._finish()

    def action_skip(self):
        self.ensure_one()
        if not self.allow_skip:
            raise UserError("Para confirmar hace falta el WhatsApp del cliente. Cargalo y tocá "
                            "«Guardar y confirmar».")
        self.order_id.check_access("write")
        self.order_id.message_post(
            body="Confirmado sin WhatsApp del cliente por %s" % self.env.user.name)
        return self._finish()

    def _finish(self):
        order = self._order_to_confirm()
        if self.then == "counter":
            result = order.action_distri_counter_sale()
        else:
            result = order.action_confirm()
        if isinstance(result, dict):
            return result
        return {"type": "ir.actions.act_window_close"}


class DistriClienteSinWhatsapp(models.Model):
    """«Clientes sin WhatsApp»: customers with confirmed orders in the last
    90 days, to fill their WhatsApp from the list. A SQL view: the counts
    are sortable; the WhatsApp column writes to the contact."""
    _name = "distri.cliente.sin.whatsapp"
    _description = "Clientes sin WhatsApp"
    _auto = False
    _order = "order_count desc, amount_total desc"
    _rec_name = "partner_id"
    # flushed before the view is read
    _depends = {
        "sale.order": ["state", "date_order", "partner_id", "amount_total", "currency_id"],
        "res.partner": ["active", "commercial_partner_id", "distri_generic_partner",
                        "distri_whatsapp"],
    }

    partner_id = fields.Many2one("res.partner", string="Cliente", readonly=True)
    distri_whatsapp = fields.Char(related="partner_id.distri_whatsapp", readonly=False,
                                  string="WhatsApp")
    phone = fields.Char(related="partner_id.phone", string="Teléfono")
    city = fields.Char(related="partner_id.city", string="Localidad")
    user_id = fields.Many2one(related="partner_id.user_id", string="Vendedor")
    order_count = fields.Integer(string="Pedidos (90 días)", readonly=True)
    amount_total = fields.Monetary(string="Comprado (90 días)", readonly=True,
                                   currency_field="currency_id")
    currency_id = fields.Many2one("res.currency", readonly=True)
    last_order_date = fields.Datetime(string="Último pedido", readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW %s AS (
                SELECT p.id AS id,
                       p.id AS partner_id,
                       COUNT(so.id) AS order_count,
                       SUM(so.amount_total) AS amount_total,
                       MIN(so.currency_id) AS currency_id,
                       MAX(so.date_order) AS last_order_date
                  FROM sale_order so
                  JOIN res_partner sp ON sp.id = so.partner_id
                  JOIN res_partner p ON p.id = COALESCE(sp.commercial_partner_id, sp.id)
                 WHERE so.state = 'sale'
                   AND so.date_order >= (NOW() AT TIME ZONE 'UTC') - INTERVAL '%s days'
                   AND p.active
                   AND p.distri_generic_partner IS NOT TRUE
                 GROUP BY p.id
            )""" % (self._table, LIST_DAYS))

    def write(self, vals):
        """Only the WhatsApp is editable: it goes to the contact (with its
        normalization and validation)."""
        if set(vals) - {"distri_whatsapp"}:
            raise UserError("En esta lista solo se carga el WhatsApp.")
        for row in self:
            row.partner_id.write({"distri_whatsapp": vals["distri_whatsapp"]})
        return True
