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

19.0.1.129.25 (owner, 7 real orders such as S00237 and S00390): «Ya lo armé y
ya se entregó». An invoiced order packed AND physically handed to the
customer outside the system: packed as with «Ya lo armé» and flagged
`distri_entregado_fuera` (who and when), so it is «Entregado»
(`distri_delivery_state`) and leaves every «to deliver» queue: route
planning and «Asignar pedidos» (`_free_orders_domain`, `_order_is_free`, so
/rutas «Sin ruta · depósito» and /despacho), and the depot delivery
(controllers/deposito.py `deliverable_domain`, `_distri_check_depot_delivery`).
Owner's rule: no payment, invoice or journal entry is created, cancelled or
reconciled, and no pending collection or charge is created (the depot
delivery would have asked «pagó / no pagó nada»).

19.0.1.129.36 (owner's decision B, 25/11/2026): with «Nueva venta» on
(distri_pedido.nueva_venta), «Facturar igual» is gone. The wizard no longer
offers it and the server refuses an order not packed yet, also with the
wizard's box or the context set by hand, pointing to «Nueva venta»
(«Mostrador» or «Ya entregado»). The orders already invoiced that way keep
«Ya lo armé» and «… y ya se entregó». Superuser code (scripts, tests) is not
affected. With the flag off it is as in 129.35.
"""
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .armado import MAX_BULTOS

NOT_PACKED = ("%s todavía no está armado. La factura tiene que salir al terminar el armado, "
              "para que descuente lo que faltó. Si igual hay que facturarlo, administración "
              "tilda «Facturar igual» en «Crear factura».")
# 19.0.1.129.36: with «Nueva venta» on there is no «Facturar igual»
SIN_FACTURAR_IGUAL = ("%s todavía no está armado. La factura sale sola al terminar el armado, "
                      "para que descuente lo que faltó. «Facturar igual» ya no existe: si el "
                      "cliente se lo lleva ahora o ya lo tiene, cargá la venta con «Nueva venta» "
                      "(«Mostrador» o «Ya entregado»).")
FORCE_GROUPS = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia",
                "base.group_system")


# 19.0.1.117.0: «Facturados sin armar» lists the orders of the last days
# only (by order date). On 19/11/2026 the whole production history had two
# such orders, both of that day; an older one is administración's to look at.
DIAS_FACTURADOS_SIN_ARMAR = 14
YA_ARMADO_GROUPS = ("distri_pedido.grupo_armador", "distri_pedido.grupo_administracion")


def can_force(user):
    """May tick «Facturar igual»: never while «Nueva venta» is on (129.36)."""
    from .nueva_venta import nueva_venta_on
    if nueva_venta_on(user.env):
        return False
    return not user.share and any(user.has_group(g) for g in FORCE_GROUPS)


def not_packed_message(env):
    from .nueva_venta import nueva_venta_on
    return SIN_FACTURAR_IGUAL if nueva_venta_on(env) else NOT_PACKED


class SaleOrderInvoiceUnpacked(models.Model):
    _inherit = "sale.order"

    def _distri_check_packed_for_invoice(self):
        if self.env.su:
            return
        from .nueva_venta import nueva_venta_on
        # 19.0.1.129.36: the context of «Facturar igual» opens nothing with
        # «Nueva venta» on
        if self.env.context.get("distri_facturar_sin_armar") and not nueva_venta_on(self.env):
            return
        unpacked = self.filtered(lambda o: o.distri_armado != "armado")
        if unpacked:
            raise UserError(not_packed_message(self.env) % ", ".join(unpacked.mapped("name")))

    def _create_invoices(self, grouped=False, final=False, date=None):
        self._distri_check_packed_for_invoice()
        return super()._create_invoices(grouped=grouped, final=final, date=date)


    # ------------------------------------------- 19.0.1.117.0: «Ya lo armé»

    def _distri_factura_viva(self):
        """Its customer invoices that are not cancelled."""
        self.ensure_one()
        return self.sudo().invoice_ids.filtered(
            lambda f: f.move_type == "out_invoice" and f.state != "cancel")

    def _distri_factura_viva_texto(self):
        """«0001/2026/08240, borrador»: the live invoices for a person to read.
        A draft has no number yet (name False): joining it raised a TypeError
        that took /armado down (19.0.1.129.21)."""
        self.ensure_one()
        return ", ".join(f.name if f.name and f.name != "/" else "borrador"
                         for f in self._distri_factura_viva())

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

    distri_entregado_fuera = fields.Boolean(
        string="Entregado fuera del sistema", readonly=True, copy=False, index=True,
        help="Ya estaba facturado y se entregó al cliente sin pasar por una ruta ni por la "
             "entrega en depósito («Ya lo armé y ya se entregó» en /armado).")
    distri_entregado_fuera_por_id = fields.Many2one(
        "res.users", string="Marcó la entrega fuera del sistema", readonly=True, copy=False)
    distri_entregado_fuera_el = fields.Datetime(
        string="Entrega fuera del sistema marcada el", readonly=True, copy=False)

    @api.depends("distri_stop_ids.delivery_state", "distri_stop_ids.unloaded",
                 "distri_entregado_fuera")
    def _compute_distri_delivery_state(self):
        """19.0.1.129.25: delivered outside the system is «Entregado»."""
        super()._compute_distri_delivery_state()
        for order in self.filtered("distri_entregado_fuera"):
            order.distri_delivery_state = "delivered"

    def distri_ya_armado(self, bultos, entregado=False, motivo=None):
        """«Ya lo armé»: an invoiced order packed outside /armado. Marks it
        packed by the current user with `bultos`, validates its depot
        delivery with the ordered quantities (nothing was marked missing)
        and creates no invoice: it has one. Returns an error text or "".

        19.0.1.129.25: `entregado` («Ya lo armé y ya se entregó») also
        marks it delivered outside the system (see the module docstring)."""
        self.ensure_one()
        user = self.env.user
        if not (self.env.su or any(user.has_group(g) for g in YA_ARMADO_GROUPS)):
            raise AccessError("Solo el armador o administración marcan un pedido como armado.")
        order = self.sudo()
        order._distri_bloquear()
        if order.distri_armado == "armado" and order.state != "cancel":
            # a double tap: the first one already did it
            same = order.distri_armador_id == user and (
                not entregado or order.distri_entregado_fuera)
            return "" if same else "Este pedido ya está armado."
        if not order._distri_es_facturado_sin_armar():
            return "Este pedido ya no está entre los facturados sin armar."
        if not bultos or bultos < 1 or bultos > MAX_BULTOS:
            return "Poné en cuántos bultos quedó el pedido."
        # 19.0.1.129.35 (plan, Etapa 4): with «Nueva venta» on, «Ya lo armé»
        # is a correction tool: it asks why
        from .nueva_venta import nueva_venta_on
        motivo = " ".join((motivo or "").split())[:250]
        if nueva_venta_on(self.env) and len(motivo) < 3:
            return "Poné el motivo de la corrección."
        nota = (" Corrección. Motivo: %s." % motivo) if motivo else ""
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
                if entregado:
                    order.write({"distri_entregado_fuera": True,
                                 "distri_entregado_fuera_por_id": user.id,
                                 "distri_entregado_fuera_el": now})
        except UserError as exc:
            return "No se pudo marcar como armado: %s" % (exc.args[0] if exc.args else exc)
        packs = "%s %s" % (bultos, "bulto" if bultos == 1 else "bultos")
        if entregado:
            order.message_post(body="Marcado como armado y entregado fuera del sistema por %s, "
                                    "en %s (ya estaba facturado: %s).%s" % (
                user.name, packs, order._distri_factura_viva_texto(), nota))
        else:
            order.message_post(body="Marcado como armado sin pasar por el armado (ya estaba "
                                    "facturado: %s) por %s, en %s.%s" % (
                order._distri_factura_viva_texto(), user.name, packs, nota))
        order._distri_ensure_receipt_code()
        return ""


class DistriRouteDeliveredOutside(models.Model):
    """19.0.1.129.25: an order delivered outside the system is no route's."""
    _inherit = "distri.route"

    def _free_orders_domain(self):
        return super()._free_orders_domain() + [("distri_entregado_fuera", "=", False)]

    def _order_is_free(self, order):
        if order.distri_entregado_fuera:
            return False
        return super()._order_is_free(order)


class SaleAdvancePaymentInvUnpacked(models.TransientModel):
    _inherit = "sale.advance.payment.inv"

    distri_sin_armar = fields.Char(string="Pedidos sin armar",
                                   compute="_compute_distri_sin_armar")
    distri_puede_forzar = fields.Boolean(compute="_compute_distri_sin_armar")
    # 19.0.1.129.36: «Nueva venta» on, so no «Facturar igual»
    distri_sin_facturar_igual = fields.Boolean(compute="_compute_distri_sin_armar")
    distri_facturar_sin_armar = fields.Boolean(
        string="Facturar igual",
        help="El pedido todavía no está armado: la factura no descuenta lo que falte.")

    @api.depends("sale_order_ids")
    def _compute_distri_sin_armar(self):
        from .nueva_venta import nueva_venta_on
        allowed = can_force(self.env.user)
        gone = nueva_venta_on(self.env)
        for wizard in self:
            wizard.distri_sin_facturar_igual = gone
            wizard.distri_sin_armar = ", ".join(wizard.sale_order_ids.filtered(
                lambda o: o.distri_armado != "armado").mapped("name"))
            wizard.distri_puede_forzar = allowed

    def create_invoices(self):
        unpacked = self.sale_order_ids.filtered(lambda o: o.distri_armado != "armado")
        if unpacked and not self.env.su:
            from .nueva_venta import nueva_venta_on
            if nueva_venta_on(self.env) or not self.distri_facturar_sin_armar:
                raise UserError(not_packed_message(self.env) % ", ".join(
                    unpacked.mapped("name")))
            if not can_force(self.env.user):
                raise UserError("Solo administración o gerencia factura un pedido sin armar.")
            for order in unpacked:
                order.message_post(body="Facturado sin armar por %s («Facturar igual»)."
                                   % self.env.user.name)
            return super(SaleAdvancePaymentInvUnpacked, self.with_context(
                distri_facturar_sin_armar=True)).create_invoices()
        return super().create_invoices()
