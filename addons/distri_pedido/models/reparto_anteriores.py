"""19.0.1.122.0 «Rutas anteriores»: the driver sees his own routes of the
last days, read only (controllers/reparto_anteriores.py).

Only routes of kind «route» whose driver is the user, dated before today
(Argentine date) and not older than PAST_DAYS. 19.0.1.129.23: also today's
routes once settled, since the app's main screen drops a settled route at
once (controllers/reparto.py) and the driver could not see it until the
next day; today's routes still open stay on the main screen only. Each stop shows what the
driver saw that day about its result: customer, town and address, delivered
or not (with the reason) and what he collected (cash, transfer). Never the
customer's balance or debt (previous_debt, expected_balance): a wholesale
customer's account stays with administración (19.0.1.120.0), and the history
shows no account of any customer.
"""
from datetime import timedelta

from odoo import api, models

from .reparto_cliente import today_ar

PAST_DAYS = 30

DELIVERY_LABELS = {
    "pending": ("Sin visitar", "warn"),
    "delivered_code": ("Entregado con código", "ok"),
    "delivered_no_code": ("Entregado sin código", "ok"),
    "not_delivered": ("No se entregó", "err"),
    "dispatched": ("Encomienda despachada", "info"),
}
DELIVERED = ("delivered_code", "delivered_no_code", "dispatched")


def money_ar(amount):
    """$ 12.345,67, the Argentine format of the driver app."""
    text = "{:,.2f}".format(amount or 0.0)
    return "$ " + text.replace(",", "_").replace(".", ",").replace("_", ".")


class DistriRoutePast(models.Model):
    _inherit = "distri.route"

    @api.model
    def _distri_past_domain(self, user):
        today = today_ar()
        return [("driver_id", "=", user.id), ("kind", "=", "route"),
                ("date", "<=", today), ("date", ">=", today - timedelta(days=PAST_DAYS)),
                "|", ("date", "<", today), ("state", "=", "settled")]

    @api.model
    def _distri_past_routes(self, user):
        """The user's own routes of the last PAST_DAYS days (today's once
        settled), newest first."""
        return self.sudo().search(self._distri_past_domain(user),
                                  order="date desc, trip desc, id desc")

    @api.model
    def _distri_past_route(self, user, route_id):
        """One of those routes, or an empty recordset (another driver's,
        today's still open, too old, a depot box or a missing id)."""
        try:
            wanted = int(route_id)
        except (TypeError, ValueError):
            return self.sudo().browse()
        if wanted <= 0:
            return self.sudo().browse()
        return self.sudo().search([("id", "=", wanted)] + self._distri_past_domain(user),
                                  limit=1)

    def _distri_past_summary(self):
        self.ensure_one()
        stops = self.stop_ids
        delivered = stops.filtered(lambda s: s.delivery_state in DELIVERED)
        not_delivered = stops.filtered(lambda s: s.delivery_state == "not_delivered")
        transfers = sum(stops.filtered(lambda s: s.transfer == "yes").mapped("transfer_amount"))
        return {
            "id": self.id,
            "name": self.name,
            "date": self.date.strftime("%d/%m/%Y"),
            "state": dict(self._fields["state"]._description_selection(self.env)).get(
                self.state, ""),
            "stops": len(stops),
            "delivered": len(delivered),
            "not_delivered": len(not_delivered),
            "cash": money_ar(self.cash_collected),
            "transfer": money_ar(transfers) if transfers else "",
        }

    def _distri_past_stops(self):
        """The stops in the order they had, with their result."""
        self.ensure_one()
        rows = []
        for stop in self.stop_ids.sorted(lambda s: (s.sequence, s.id)):
            partner = stop.partner_id
            ship = stop.shipping_partner_id or partner
            label, kind = DELIVERY_LABELS.get(stop.delivery_state, ("", "info"))
            rows.append({
                "name": partner._distri_app_name() if partner else "",
                "town": partner._distri_town() if partner else "",
                "address": ", ".join(p for p in (ship.street, ship.street2) if p),
                "order": stop.order_id.name,
                "result": label,
                "kind": kind,
                "reason": (stop.not_delivered_reason or "")
                if stop.delivery_state == "not_delivered" else "",
                "cash": money_ar(stop.cash_amount) if stop.cash_amount else "",
                "transfer": money_ar(stop.transfer_amount)
                if stop.transfer == "yes" and stop.transfer_amount else "",
                "comment": stop.comment or "",
            })
        return rows
