"""«Anular ruta» (19.0.1.113.0, owner's tests of 18-19/11/2026).

A route made by mistake, or to test the driver's app, may already have
visits recorded from the phone. Administración or gerencia cancel it from
the route form: its stops are undone (the visit is dropped, the order is free
again for another route, each order says so in its chatter) and the route is
deleted, so nothing of it reaches the settlement, the planning board or the
driver's phone (the app moves to the driver's current route and its pending
changes end in «Cambios rechazados», controllers/reparto.py).

Never with money: a route that registered cash, transfers, payments, depot
cash, a caja movement, an expense, a dispatched parcel, a charge to the
driver or a return checked at the depot is refused with the list of what it
has, because undoing it would hide money. That route is settled as usual.

19.0.1.115.0 (owner): only a route still «Asignada» (draft) is cancelled; one
«En reparto» or closed on the phone is refused, the driver's mistakes are
fixed with «Reabrir para el repartidor» and the route is settled.
"""
from markupsafe import Markup

from odoo import fields, models
from odoo.exceptions import UserError

CANCEL_GROUPS = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia",
                 "base.group_system")


class DistriRouteCancel(models.Model):
    _inherit = "distri.route"

    def _distri_cancel_money(self):
        """What money the route has, as short Spanish phrases (empty: none)."""
        self.ensure_one()
        env = self.env
        found = []
        stops = self.stop_ids
        if self.state == "settled":
            found.append("ya se rindió")
        if self.payment_ids:
            found.append("pagos registrados")
        cash = sum(stops.mapped("cash_amount"))
        if cash:
            found.append("efectivo cobrado en las visitas")
        if sum(stops.mapped("transfer_amount")):
            found.append("transferencias informadas en las visitas")
        if self.sudo().cash_collection_ids:
            found.append("cobros en efectivo")
        # 19.0.1.129.27: an «Adelanto para la ruta» is money of the route too
        if env["distri.caja.movimiento"].sudo().search_count(
                ["|", "|", ("route_id", "=", self.id), ("box_id", "=", self.id),
                 ("advance_route_id", "=", self.id)]):
            found.append("movimientos de caja")
        if self.sudo().expense_ids:
            found.append("gastos")
        if env["distri.parcel"].sudo().search_count([("stop_id", "in", stops.ids)]):
            found.append("encomiendas despachadas")
        if env["distri.employee.debt"].sudo().search_count([("route_id", "=", self.id)]):
            found.append("cargos al repartidor")
        if env["distri.whatsapp.message"].sudo().search_count(
                [("distri_app_stop_id", "in", stops.ids)]):
            found.append("comprobantes de transferencia")
        if stops.return_ids.filtered(lambda r: r.settlement_state != "pending"):
            found.append("devoluciones controladas en el depósito")
        if env["distri.sync.rejection"].sudo().search_count([
                ("route_id", "=", self.id), ("state", "=", "open"),
                "|", ("cash", "!=", 0), ("amount", "!=", 0)]):
            found.append("cambios rechazados con plata")
        return found

    def _distri_check_can_cancel(self):
        user = self.env.user
        if not (self.env.su or any(user.has_group(g) for g in CANCEL_GROUPS)):
            raise UserError("Solo administración o gerencia anulan una ruta.")
        for route in self:
            if route.kind != "route":
                raise UserError("Solo se anulan rutas de reparto.")
            # 19.0.1.115.0 (owner): only a route that never left the depot
            if route.state in ("on_route", "closed"):
                raise UserError(
                    "No se puede anular %s: ya salió a reparto (%s). Para corregir una visita "
                    "usá «Reabrir para el repartidor»; la ruta se rinde en la caja como "
                    "siempre." % (route.name, dict(route._fields["state"].selection).get(
                        route.state, route.state)))
            money = route._distri_cancel_money()
            if money:
                raise UserError(
                    "No se puede anular %s: tiene plata (%s). Una ruta con plata se rinde como "
                    "siempre; si algo está mal, corregilo en la rendición." % (
                        route.name, ", ".join(money)))

    def action_distri_cancel_route(self):
        """«Anular ruta»: undo the stops, free the orders and delete the route."""
        self._distri_check_can_cancel()
        who = self.env.user.name
        for route in self.sudo():
            name = route.name
            stops = route.stop_ids
            for stop in stops:
                visit = dict(stop._fields["state"].selection).get(stop.state, "")
                note = Markup("Se anuló la ruta <b>%s</b> (la anuló %s). ") % (name, who)
                if stop.state != "pending":
                    note += Markup("La visita registrada (%s) se deshizo. ") % visit
                note += Markup("El pedido quedó libre para otra ruta.")
                stop.order_id.message_post(body=note)
            # a rejected change of this route stays for the record, resolved
            open_rejections = self.env["distri.sync.rejection"].sudo().search(
                [("route_id", "=", route.id), ("state", "=", "open")])
            if open_rejections:
                open_rejections.write({
                    "state": "resolved", "resolved_by_id": self.env.user.id,
                    "resolved_at": fields.Datetime.now(),
                    "resolution": "Se anuló la ruta %s (la anuló %s)." % (name, who)})
            # receipts photographed at a visit are money: refused above; the
            # pending receipts («No pagó», without an amount) go with the stop
            stops.with_context(distri_route_cancel=True).unlink()
            route.unlink()
        return self.env["ir.actions.act_window"]._for_xml_id("distri_pedido.action_route")

