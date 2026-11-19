"""19.0.1.115.0 (owner, found live): an order is invoiced when its packing
ends (models/factura_armado.py), so the invoice discounts what was missing.
The standard «Crear factura» of the backend on an order not packed yet is
refused with that explanation; administración and gerencia can still tick
«Facturar igual» in the wizard (context distri_facturar_sin_armar).

The module's own invoices are not affected: the one at the end of packing
and the counter sale (/deposito/mostrador, packed and invoiced in one step)
are made after the order is «Armado». Superuser code (scripts, tests) is not
affected either.
"""
from odoo import api, fields, models
from odoo.exceptions import UserError

NOT_PACKED = ("%s todavía no está armado. La factura tiene que salir al terminar el armado, "
              "para que descuente lo que faltó. Si igual hay que facturarlo, administración "
              "tilda «Facturar igual» en «Crear factura».")
FORCE_GROUPS = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia",
                "base.group_system")


def can_force(user):
    return not user.share and any(user.has_group(g) for g in FORCE_GROUPS)


class SaleOrderInvoiceUnpacked(models.Model):
    _inherit = "sale.order"

    def _distri_check_packed_for_invoice(self):
        if self.env.su or self.env.context.get("distri_facturar_sin_armar"):
            return
        unpacked = self.filtered(lambda o: o.distri_armado != "armado")
        if unpacked:
            raise UserError(NOT_PACKED % ", ".join(unpacked.mapped("name")))

    def _create_invoices(self, grouped=False, final=False, date=None):
        self._distri_check_packed_for_invoice()
        return super()._create_invoices(grouped=grouped, final=final, date=date)


class SaleAdvancePaymentInvUnpacked(models.TransientModel):
    _inherit = "sale.advance.payment.inv"

    distri_sin_armar = fields.Char(string="Pedidos sin armar",
                                   compute="_compute_distri_sin_armar")
    distri_puede_forzar = fields.Boolean(compute="_compute_distri_sin_armar")
    distri_facturar_sin_armar = fields.Boolean(
        string="Facturar igual",
        help="El pedido todavía no está armado: la factura no descuenta lo que falte.")

    @api.depends("sale_order_ids")
    def _compute_distri_sin_armar(self):
        allowed = can_force(self.env.user)
        for wizard in self:
            wizard.distri_sin_armar = ", ".join(wizard.sale_order_ids.filtered(
                lambda o: o.distri_armado != "armado").mapped("name"))
            wizard.distri_puede_forzar = allowed

    def create_invoices(self):
        unpacked = self.sale_order_ids.filtered(lambda o: o.distri_armado != "armado")
        if unpacked and not self.env.su:
            if not self.distri_facturar_sin_armar:
                raise UserError(NOT_PACKED % ", ".join(unpacked.mapped("name")))
            if not can_force(self.env.user):
                raise UserError("Solo administración o gerencia factura un pedido sin armar.")
            for order in unpacked:
                order.message_post(body="Facturado sin armar por %s («Facturar igual»)."
                                   % self.env.user.name)
            return super(SaleAdvancePaymentInvUnpacked, self.with_context(
                distri_facturar_sin_armar=True)).create_invoices()
        return super().create_invoices()
