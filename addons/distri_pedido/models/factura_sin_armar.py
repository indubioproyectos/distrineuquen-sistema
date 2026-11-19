"""19.0.1.115.0 (owner, found live): an order is invoiced when its packing
ends (models/factura_armado.py), so the invoice discounts what was missing.
The standard «Crear factura» of the backend on an order not packed yet is
refused with that explanation; administración and gerencia can still tick
«Facturar igual» in the wizard (context distri_facturar_sin_armar).

The module's own invoices are not affected: the one at the end of packing
and the counter sale (/deposito/mostrador, packed and invoiced in one step)
are made after the order is «Armado». Superuser code (scripts, tests) is not
affected either.

19.0.1.117.0 (owner, real case S00239): an order invoiced by hand before it
was packed left /armado (`_distri_sin_entregar` is False once it has an
invoice) and never reached route planning (it needs «Armado»). /armado now
lists those orders apart, «Facturados sin armar» (`_distri_facturados_sin_
armar`), and «Ya lo armé» (`distri_ya_armado`) marks one packed with its
bultos and validates its depot delivery as ordered, without a second invoice.
"""
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .armado import MAX_BULTOS

NOT_PACKED = ("%s todavía no está armado. La factura tiene que salir al terminar el armado, "
              "para que descuente lo que faltó. Si igual hay que facturarlo, administración "
              "tilda «Facturar igual» en «Crear factura».")
FORCE_GROUPS = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia",
                "base.group_system")


# 19.0.1.117.0: «Facturados sin armar» lists the orders of the last days
# only (by order date). On 19/11/2026 the whole production history had two
# such orders, both of that day; an older one is administración's to look at.
DIAS_FACTURADOS_SIN_ARMAR = 14
YA_ARMADO_GROUPS = ("distri_pedido.grupo_armador", "distri_pedido.grupo_administracion")


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


    # ------------------------------------------- 19.0.1.117.0: «Ya lo armé»

    def _distri_factura_viva(self):
        """Its customer invoices that are not cancelled."""
        self.ensure_one()
        return self.sudo().invoice_ids.filtered(
            lambda f: f.move_type == "out_invoice" and f.state != "cancel")

    def _distri_es_facturado_sin_armar(self):
        """Confirmed, of a real customer, released, not waiting for its
        WhatsApp, never packed, and already invoiced."""
        self.ensure_one()
        return (self.state == "sale" and self.distri_armado == "pendiente"
                and not self.distri_retenido and self.distri_wa_state != "waiting"
                and not self._distri_sin_cliente() and bool(self._distri_factura_viva()))

    @api.model
    def _distri_facturados_sin_armar(self):
        """The «Facturados sin armar» block of /armado, oldest first. The
        hidden lists and wholesale customers are not filtered: the packer
        always saw them."""
        since = fields.Datetime.now() - timedelta(days=DIAS_FACTURADOS_SIN_ARMAR)
        orders = self.sudo().search([
            ("state", "=", "sale"), ("distri_armado", "=", "pendiente"),
            ("distri_retenido", "=", False), ("distri_wa_state", "!=", "waiting"),
            ("partner_id.distri_generic_partner", "=", False),
            ("company_id", "=", self.env.company.id), ("date_order", ">=", since),
        ], order="date_order, id")
        return orders.filtered(lambda o: o._distri_es_facturado_sin_armar())

    def distri_ya_armado(self, bultos):
        """«Ya lo armé»: an invoiced order packed outside /armado. Marks it
        packed by the current user with `bultos`, validates its depot
        delivery with the ordered quantities (nothing was marked missing)
        and creates no invoice: it has one. Returns an error text or ""."""
        self.ensure_one()
        user = self.env.user
        if not (self.env.su or any(user.has_group(g) for g in YA_ARMADO_GROUPS)):
            raise AccessError("Solo el armador o administración marcan un pedido como armado.")
        order = self.sudo()
        order._distri_bloquear()
        if order.distri_armado == "armado" and order.state != "cancel":
            # a double tap: the first one already did it
            return "" if order.distri_armador_id == user else "Este pedido ya está armado."
        if not order._distri_es_facturado_sin_armar():
            return "Este pedido ya no está entre los facturados sin armar."
        if not bultos or bultos < 1 or bultos > MAX_BULTOS:
            return "Poné en cuántos bultos quedó el pedido."
        invoices = order._distri_factura_viva()
        now = fields.Datetime.now()
        try:
            with self.env.cr.savepoint():
                order.with_context(distri_armado=True).write({
                    "distri_armado": "armado",
                    "distri_armado_inicio": now,
                    "distri_armado_fin": now,
                    "distri_armador_id": user.id,
                    "distri_bultos": bultos,
                })
                order._distri_entregar_lo_armado()
        except UserError as exc:
            return "No se pudo marcar como armado: %s" % (exc.args[0] if exc.args else exc)
        order.message_post(body="Marcado como armado sin pasar por el armado (ya estaba "
                                "facturado: %s) por %s, en %s %s." % (
            ", ".join(invoices.mapped("name")), user.name, bultos,
            "bulto" if bultos == 1 else "bultos"))
        order._distri_ensure_receipt_code()
        return ""


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
