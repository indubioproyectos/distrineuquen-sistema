"""«Pagado / comprobante cargado» in the driver's visit (19.0.1.129.16).

The visit tells the driver that the customer already paid by transfer, or
that a receipt was loaded and nobody credited it yet: `payments` in
`distri.route.stop._app_payload` and in the load poll
(`distri.route._load_status_payload`, /reparto/api/load), and the receipt
itself through /reparto/api/receipt (controllers/reparto.py). Owner's
decision 2: it only informs. Nothing here changes a balance or credits
anything.

Which receipts (cards `distri.mp.collection` of kind «verify» or «receipt»,
never a discarded one nor a credited one annulled or reverted in the audit),
from the surest to the least sure:

1. the ones tied to the stop's order: uploaded by the customer from the
   order page (`distri_customer_order_id`), photographed in the app at this
   order or stop (`distri_app_order_id`, `distri_app_stop_id`), sent with the
   order's code (`distri_order_id`), or paying one of its invoices
   (`pay_move_id`);
2. else the customer's receipts loaded since the stop exists (the date rule
   of `_compute_paid_elsewhere`).

A wholesale customer gets nothing, like «Ver cuenta» (`_app_account_visible`).
"""
from odoo import models

from .cobros import local_day, local_hour
from .despacho import first_name
from .reparto_cliente import today_ar

PAYMENT_KINDS = ("verify", "receipt")
# a credited payment undone in the audit is no payment for the driver
VOID_AUDIT_STATES = ("anulado", "revertido")
PAYMENTS_SHOWN = 5
PAID_TOLERANCE = 1.0
CHANNELS = {
    "app": "app de reparto",
    "order": "página del pedido",
    "staff": "carga del personal",
    "whatsapp": "WhatsApp",
}


class DistriRouteStopPayments(models.Model):
    _inherit = "distri.route.stop"

    def _app_payment_cards(self):
        """The cards shown in the visit, newest first (see the module
        docstring). Empty for a wholesale customer."""
        self.ensure_one()
        Card = self.env["distri.mp.collection"].sudo()
        if not self._app_account_visible():
            return Card
        stop = self.sudo()
        order = stop.order_id
        base = [("kind", "in", PAYMENT_KINDS), ("state", "!=", "ignored"),
                "|", ("audit_state", "=", False), ("audit_state", "not in", VOID_AUDIT_STATES)]
        tied = ["|", "|", "|", "|",
                ("receipt_id.distri_customer_order_id", "=", order.id),
                ("receipt_id.distri_app_order_id", "=", order.id),
                ("receipt_id.distri_app_stop_id", "=", stop.id),
                ("receipt_id.distri_order_id", "=", order.id),
                ("pay_move_id", "in", order._distri_facturas_emitidas().ids)]
        cards = Card.search(base + tied, order="id desc", limit=PAYMENTS_SHOWN)
        partner = stop.partner_id.commercial_partner_id
        if cards or not stop.create_date or not partner or partner.distri_generic_partner:
            return cards
        return Card.search(base + [
            ("partner_id.commercial_partner_id", "=", partner.id),
            "|", ("create_date", ">=", stop.create_date),
            ("credited_at", ">=", stop.create_date)], order="id desc", limit=PAYMENTS_SHOWN)

    @staticmethod
    def _app_payment_channel(receipt):
        if receipt.distri_from_app or receipt.distri_app_stop_id:
            return "app"
        if receipt.distri_customer_order_id:
            return "order"
        if receipt.distri_forwarded_by_id:
            return "staff"
        return "whatsapp"

    def _app_payment_item(self, card):
        receipt = card.receipt_id
        credited = card.state == "confirmed"
        when = (card.credited_at if credited and card.credited_at else
                receipt.create_date or card.create_date)
        if credited:
            by = card.credited_by_id
        else:
            by = receipt.distri_forwarded_by_id or receipt.distri_app_stop_id.route_id.driver_id
        channel = self._app_payment_channel(receipt)
        day = local_day(when)
        return {
            "receipt_id": card.id,
            "state": "acreditado" if credited else "sin_acreditar",
            "amount": card.amount,
            "when": when.isoformat(sep=" ", timespec="seconds") if when else "",
            # «14:05» today, «21/11 14:05» another day
            "when_label": ("%s%s" % ("" if day == today_ar() else day.strftime("%d/%m "),
                                     local_hour(when))) if when else "",
            "by": first_name(by) or ("el cliente" if channel == "order" else ""),
            "channel": channel,
            "channel_label": CHANNELS[channel],
            "audit_state": card.audit_state or "",
            "verified": card.audit_state == "verificado",
            "has_file": bool(receipt.attachment_id),
        }

    def _app_payments(self):
        """{"payments": [...], "due": float, "paid": bool} of the visit.
        `due` is what is still to collect for the order now (the invoiced
        residual; without an invoice, the order total minus the payments
        credited to this order); `paid` when a credited payment left
        nothing to collect."""
        self.ensure_one()
        cards = self._app_payment_cards()
        if not cards:
            return {"payments": [], "due": 0.0, "paid": False}
        order = self.sudo().order_id
        if order._distri_facturas_emitidas():
            due = order._distri_amount_to_collect()
        else:
            credited = cards.filtered(lambda c: c.state == "confirmed" and (
                order in (c.receipt_id.distri_customer_order_id | c.receipt_id.distri_app_order_id
                          | c.receipt_id.distri_order_id)
                or c.receipt_id.distri_app_stop_id == self))
            due = order.amount_total - sum(credited.mapped("amount"))
        due = max(round(due, 2), 0.0)
        any_credited = any(c.state == "confirmed" for c in cards)
        return {
            "payments": [self._app_payment_item(card) for card in cards],
            "due": due,
            "paid": any_credited and due <= PAID_TOLERANCE,
        }

    def _app_receipt_file(self, card_id):
        """(bytes, mimetype, filename) of a receipt shown in this visit, or
        None: only the cards of `_app_payment_cards`."""
        self.ensure_one()
        try:
            wanted = int(card_id or 0)
        except (TypeError, ValueError):
            return None
        card = self._app_payment_cards().filtered(lambda c: c.id == wanted)
        attachment = card.receipt_id.attachment_id.sudo() if card else False
        if not attachment:
            return None
        return (attachment.raw, attachment.mimetype or "application/octet-stream",
                attachment.name or "comprobante")

    def _app_payload(self):
        data = super()._app_payload()
        info = self._app_payments()
        data.update({"payments": info["payments"], "payments_due": info["due"],
                     "payments_paid": info["paid"]})
        return data


class DistriRoutePayments(models.Model):
    _inherit = "distri.route"

    def _load_status_payload(self):
        """The poll also brings the payments of each visit, so the notice
        shows without «Actualizar la ruta»."""
        data = super()._load_status_payload()
        data["payments"] = {str(stop.id): stop._app_payments() for stop in self.stop_ids}
        return data
