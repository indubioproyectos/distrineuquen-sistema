"""Zones whose customers pick up at the depot (19.0.1.30.0).

Management decided on 22/10/2026 that Catriel (contact prefix «W - ») is not
delivered nor shipped: its customers pick up at the depot, with the existing
«Entrega en depósito» flow. A zone marked «Retiran en el depósito»:

- puts its orders in «Lo voy a retirar» (`distri_entrega = retira`), so they
  never go on a route (reparto.py excludes them) and are delivered with
  «Entrega en depósito» (deposito.py);
- never sends them by parcel, not even when the customer typed that town in
  «Soy del interior».
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError

# what decides the order's zone (armado.py, encomienda.py)
ZONE_KEYS = {"partner_id", "partner_shipping_id", "distri_domicilio",
             "distri_encomienda_localidad", "distri_encomienda_pedida", "distri_entrega"}


class DistriZonaRetiro(models.Model):
    _inherit = "distri.zona"

    retira_deposito = fields.Boolean(
        string="Retiran en el depósito",
        help="Los clientes de esta zona retiran sus pedidos en el depósito: no se reparten ni "
             "salen por encomienda. Los pedidos quedan en «Lo voy a retirar» y se entregan con "
             "«Entrega en depósito».")

    @api.constrains("retira_deposito", "encomienda")
    def _check_retira_deposito(self):
        for zona in self:
            if zona.retira_deposito and zona.encomienda:
                raise ValidationError(
                    "La zona %s no puede retirar en el depósito y salir por encomienda a la vez."
                    % zona.name)

    def write(self, vals):
        result = super().write(vals)
        if vals.get("retira_deposito"):
            orders = self.env["sale.order"].sudo().search([
                ("state", "in", ("draft", "sent", "sale")),
                ("distri_entrega", "!=", "retira"),
                ("distri_parcel_dispatched", "=", False),
            ]).filtered(lambda o: o._distri_pickup_zone() in self)
            orders._distri_apply_pickup_zone()
        return result


class SaleOrderRetiro(models.Model):
    _inherit = "sale.order"

    def _distri_pickup_zone(self):
        """The order's zone, or the town typed for a parcel, when its
        customers pick up at the depot; else an empty recordset."""
        self.ensure_one()
        Zona = self.env["distri.zona"]
        zones = self._distri_route_zone()
        if self.distri_encomienda_localidad:
            zones |= Zona.sudo().zona_de_ciudad(self.distri_encomienda_localidad)
        return zones.filtered("retira_deposito")[:1]

    def _distri_encomienda_rule(self):
        if self._distri_pickup_zone():
            return False
        return super()._distri_encomienda_rule()

    def _distri_apply_pickup_zone(self):
        """«Lo voy a retirar» for the orders of a pickup zone that are not on
        their way yet (on a route, delivered or dispatched)."""
        todo = self.filtered(lambda o: o.distri_entrega != "retira" and o._distri_pickup_zone()
                             and o.distri_delivery_state not in ("delivered", "dispatched")
                             and not o.distri_stop_ids.filtered(
                                 lambda s: s.state == "pending" and not s.unloaded))
        if todo:
            todo.with_context(distri_pickup_zone=True).write({"distri_entrega": "retira"})
            todo._distri_recompute_parcel_rule()
            for order in todo:
                order.message_post(body="Los clientes de %s retiran en el depósito: el pedido "
                                        "quedó en «Lo voy a retirar»." % (
                                            order._distri_pickup_zone().name))
        return todo

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        orders._distri_apply_pickup_zone()
        return orders

    def write(self, vals):
        result = super().write(vals)
        if ZONE_KEYS & set(vals) and not self.env.context.get("distri_pickup_zone"):
            self._distri_apply_pickup_zone()
        return result


class DistriRouteRetiro(models.Model):
    _inherit = "distri.route"

    def _order_is_free(self, order):
        """A pickup zone's order never goes on a route."""
        if order._distri_pickup_zone():
            return False
        return super()._order_is_free(order)
