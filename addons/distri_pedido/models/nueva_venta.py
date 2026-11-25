"""19.0.1.129.30: «Nueva venta», stage 1 of the owner-approved proposal
(«una pantalla, cuatro salidas»; plan unificado, Etapa 4).

One kit screen (controllers/nueva_venta.py, /nueva-venta) where staff pick
the customer, fill the cart and choose how the sale goes on. Every exit
reuses the path that already exists for it, so the records are the same:

    armar        «A armar / reparto»: the order of /pedido taken by staff
                 (controllers/main.py enviar, the same code) and confirmed:
                 it goes to /armado like any confirmed order.
    presupuesto  «Presupuesto»: the same /pedido order, left as a quotation.
    mostrador    «Mostrador»: a backend quotation and «Venta en mostrador»
                 (models/deposito.py action_distri_counter_sale): packed as
                 ordered, invoiced, then the depot delivery form with the
                 payment, as today.
    entregado    «Ya entregado»: a backend quotation, confirmed and packed as
                 ordered by the packer's own steps (the invoice comes out at
                 the end of the packing, as always), then flagged delivered
                 outside the system (19.0.1.129.25 distri_entregado_fuera)
                 with the date and the reason; its remito is dated that day.
                 It never enters route planning nor the depot delivery.

Who: «Mostrador» and «Ya entregado» are for administración (gerencia
implies it): Gonzalo, Denis and Ale. Sellers get «A armar» and
«Presupuesto»; an external seller only for his own customers (19.0.1.129.20).

Everything is behind the parameter distri_pedido.nueva_venta, OFF by
default: with it off there is no screen, no tile and nothing changes.
"""
from datetime import datetime, time

import pytz

from odoo import api, fields, models
from odoo.exceptions import UserError

from .deposito import ADMIN_GROUP, _check_admin

PARAM_NUEVA_VENTA = "distri_pedido.nueva_venta"
ON_VALUES = ("1", "true", "yes", "si", "sí", "on")

ARMAR = "armar"
MOSTRADOR = "mostrador"
ENTREGADO = "entregado"
PRESUPUESTO = "presupuesto"
# (key, label, help) in the order the screen shows them
SALIDAS = (
    (ARMAR, "A armar / reparto",
     "Se confirma y va a Armado, como un pedido tomado y confirmado."),
    (MOSTRADOR, "Mostrador",
     "El cliente está presente y se lo lleva ya: se arma, se factura y se registra el cobro."),
    (ENTREGADO, "Ya entregado",
     "La mercadería ya se entregó: se arma, se factura y queda entregada en la fecha que "
     "pongas."),
    (PRESUPUESTO, "Presupuesto",
     "Queda como presupuesto, sin confirmar todavía."),
)
SALIDAS_ADMIN = (MOSTRADOR, ENTREGADO)
# 19.0.1.129.30 (owner): the reason is mandatory and starts with this text
MOTIVO_ENTREGADO = "Ya entregado"
# the remito of «Ya entregado» is dated at noon (Argentina) of the chosen day
AR_TZ = pytz.timezone("America/Argentina/Buenos_Aires")
MEDIODIA = time(12, 0)


def nueva_venta_on(env):
    """19.0.1.129.30: the «Nueva venta» screen and its Inicio tile."""
    value = env["ir.config_parameter"].sudo().get_param(PARAM_NUEVA_VENTA) or "0"
    return str(value).strip().lower() in ON_VALUES


def puede_nueva_venta(user):
    """Staff who take orders (/pedido): sellers, administración, gerencia."""
    from .pedido_whatsapp import is_staff
    return is_staff(user)


def salidas_de(user):
    """The exits this user may choose, in screen order."""
    if not puede_nueva_venta(user):
        return []
    admin = user.has_group(ADMIN_GROUP)
    return [key for key, _label, _help in SALIDAS if admin or key not in SALIDAS_ADMIN]


def unidad_de_venta(plantilla):
    """The unit the catalog sells in (controllers/main.py _unidad_de_venta)."""
    return plantilla.uom_ids[:1] or plantilla.uom_id


class SaleOrderNuevaVenta(models.Model):
    _inherit = "sale.order"

    # 19.0.1.129.30: «Ya entregado» of «Nueva venta» (who and when it was
    # marked are distri_entregado_fuera_por_id / _el, 19.0.1.129.25)
    distri_entregado_fuera_fecha = fields.Date(
        string="Entregado fuera del sistema el día", readonly=True, copy=False,
        help="El día en que la mercadería se entregó («Ya entregado» de «Nueva venta»).")
    distri_entregado_fuera_motivo = fields.Char(
        string="Motivo de la entrega fuera del sistema", readonly=True, copy=False)

    @api.model
    def _distri_nueva_venta_presupuesto(self, partner, renglones):
        """A quotation as «Ventas › Presupuesto» makes it, as the current
        user: the customer, its price list and salesperson by Odoo's own
        defaults, each line in the catalog's unit of sale and priced by the
        list. `renglones` = [(product.template, quantity)]."""
        lineas = []
        for plantilla, cantidad in renglones:
            lineas.append((0, 0, {
                "product_id": plantilla.product_variant_id.id,
                "product_uom_qty": float(cantidad),
                "product_uom_id": unidad_de_venta(plantilla).id,
            }))
        return self.create({"partner_id": partner.id, "order_line": lineas})

    def _distri_nueva_venta_mostrador(self):
        """«Mostrador»: the depot page's «Venta en mostrador» (controllers/
        deposito.py mostrador). Returns the delivery wizard's action."""
        self.ensure_one()
        action = self.action_distri_counter_sale()
        if (action or {}).get("res_model") != "distri.depot.delivery":
            raise UserError("No se pudo hacer la venta en mostrador: falta el WhatsApp del "
                            "cliente. Cargalo en el contacto y probá de nuevo.")
        return action

    @api.model
    def _distri_fecha_entregado(self, fecha):
        """The day of «Ya entregado»: a date, today or before (owner: any
        earlier date)."""
        from .reparto_cliente import today_ar
        if not fecha:
            raise UserError("Poné el día en que se entregó.")
        try:
            dia = fields.Date.to_date(fecha)
        except (TypeError, ValueError):
            raise UserError("Revisá el día en que se entregó.") from None
        if not dia:
            raise UserError("Poné el día en que se entregó.")
        if dia > today_ar():
            raise UserError("El día de la entrega no puede ser posterior a hoy.")
        return dia

    def _distri_nueva_venta_ya_entregado(self, fecha, motivo):
        """«Ya entregado»: confirm, pack as ordered (the packer's steps, as
        «Venta en mostrador»: remito validated, invoice and receipt code as
        usual), then delivered outside the system on `fecha` with `motivo`.
        No payment, collection or charge is created."""
        self.ensure_one()
        _check_admin(self.env)
        dia = self._distri_fecha_entregado(fecha)
        motivo = " ".join((motivo or "").split())[:250]
        if not motivo:
            raise UserError("Poné el motivo de la entrega fuera del sistema.")
        if self.state not in ("draft", "sent"):
            raise UserError("«Ya entregado» sale de un presupuesto sin confirmar.")
        if self._distri_sin_cliente():
            raise UserError("Asigná el cliente real antes de cargar la venta.")
        if not self.order_line.filtered(lambda l: not l.display_type and l.product_uom_qty):
            raise UserError("La venta no tiene productos.")
        result = self.action_confirm()
        if isinstance(result, dict):
            raise UserError("No se pudo confirmar la venta: falta el WhatsApp del cliente.")
        error = self.distri_tomar() or self.distri_terminar(1)
        if error:
            raise UserError(error)
        if self.distri_factura_error:
            raise UserError("No salió la factura: %s" % self.distri_factura_error)
        # 19.0.1.129.32: invoicing at the end of packing may do nothing
        # without an error (held order, nothing to invoice): never marked
        # delivered without its invoice and its remito (everything rolls back)
        if not self.sudo().invoice_ids.filtered(lambda m: m.state != "cancel"):
            raise UserError("No salió la factura de la venta: no se cargó como entregada. "
                            "Revisala en Odoo.")
        if not self.sudo().picking_ids.filtered(lambda p: p.state == "done"):
            raise UserError("No quedó hecho el remito de la venta: no se cargó como entregada. "
                            "Revisala en Odoo.")
        user = self.env.user
        self.sudo().write({
            "distri_entregado_fuera": True,
            "distri_entregado_fuera_por_id": user.id,
            "distri_entregado_fuera_el": fields.Datetime.now(),
            "distri_entregado_fuera_fecha": dia,
            "distri_entregado_fuera_motivo": motivo,
        })
        self._distri_fechar_remito(dia)
        self.message_post(body="Venta cargada como «Ya entregado» por %s: entregada el %s. "
                               "Motivo: %s." % (user.name, dia.strftime("%d/%m/%Y"), motivo))
        return True

    def _distri_fechar_remito(self, dia):
        """The validated remito of «Ya entregado» carries the day of the
        delivery (noon, Argentina) instead of the moment it was loaded.
        Only the dates of the stock documents change: quantities, invoice
        and journal entries stay as they are."""
        from .reparto_cliente import today_ar
        if dia >= today_ar():
            return
        momento = AR_TZ.localize(datetime.combine(dia, MEDIODIA)).astimezone(
            pytz.utc).replace(tzinfo=None)
        pickings = self.sudo().picking_ids.filtered(lambda p: p.state == "done")
        # 19.0.1.129.32: the remito keeps a trace of the date it really had
        for picking in pickings:
            picking.message_post(body="Fecha del remito cambiada del %s al %s por «Ya entregado» "
                                      "(%s)." % (
                fields.Datetime.context_timestamp(picking, picking.date_done).strftime(
                    "%d/%m/%Y %H:%M") if picking.date_done else "-",
                dia.strftime("%d/%m/%Y"), self.env.user.name))
        pickings.write({"date_done": momento})
        pickings.move_ids.filtered(lambda m: m.state == "done").write({"date": momento})
        pickings.move_line_ids.write({"date": momento})
