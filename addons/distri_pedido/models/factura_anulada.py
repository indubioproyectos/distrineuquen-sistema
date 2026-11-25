"""19.0.1.129.26 (#97, part 1): cancelling the invoice of a packed or
delivered order is made visible.

Cancelling a customer invoice leaves its order packed and its depot delivery
(remito) done, with no word about it: on 24/11/2026 PAINEN BELEN's S00212
stayed packed and out of stock after its invoice was cancelled
(scripts/correcciones/painen_cancelar_s00212_body.py).

Now:
- the invoice form shows a warning, and its «Cancelar» button asks for a
  confirmation, while the order is packed or delivered
  (views/factura_anulada.xml);
- after the cancel, the order gets a chatter note and administración and
  gerencia a to-do (the same people as «Revisar la factura del armado»,
  models/factura_armado.py).

Only visibility: the cancel is never blocked, the order is never unpacked
and nothing is returned to stock. No payment, invoice or journal entry is
created, cancelled or reconciled here beyond what Odoo's own cancel does.

«Rearmar» (models/rearmar.py) cancels the invoice and the order itself on
purpose: it passes SKIP_CONTEXT and gets no note.
"""
from odoo import api, fields, models

SKIP_CONTEXT = "distri_skip_cancel_notice"

DELIVERED_STOP_STATES = ("delivered_code", "delivered_no_code", "dispatched")

NOTE = ("La factura %s se anuló; el pedido sigue armado y su remito validado. "
        "Revisar si hay que desarmar o devolver al stock.")
ACTIVITY_SUMMARY = "Factura anulada de un pedido armado"
CONFIRM = ("El pedido de esta factura ya está armado o entregado y su remito está validado. "
           "Anular la factura no desarma el pedido ni devuelve la mercadería al stock. "
           "¿Anular igual?")


class SaleOrderCancelNotice(models.Model):
    _inherit = "sale.order"

    def _distri_packed_or_delivered(self):
        """Packed, a depot delivery done, or delivered on a route or by
        courier (sudo)."""
        self.ensure_one()
        order = self.sudo()
        if order.state == "cancel":
            return False
        if order.distri_armado == "armado":
            return True
        if order.picking_ids.filtered(
                lambda p: p.state == "done" and p.picking_type_code == "outgoing"):
            return True
        if order.distri_parcel_ids:
            return True
        return bool(order.distri_stop_ids.filtered(
            lambda s: s.delivery_state in DELIVERED_STOP_STATES))

    def _distri_notify_cancelled_invoice(self, invoice_name):
        """Chatter note and one to-do per person of administración and
        gerencia, once per order and person while it is open."""
        note = NOTE % invoice_name
        for order in self.sudo().with_context(mail_activity_quick_update=True):
            order.message_post(body=note)
            open_for = order.activity_ids.filtered(
                lambda a: a.summary == ACTIVITY_SUMMARY).user_id
            for user in order._distri_avisar_factura_a() - open_for:
                order.activity_schedule(
                    "mail.mail_activity_data_todo",
                    summary=ACTIVITY_SUMMARY, note=note, user_id=user.id)


class AccountMoveCancelNotice(models.Model):
    _inherit = "account.move"

    distri_cancel_warning = fields.Boolean(
        string="Pedido armado o entregado", compute="_compute_distri_cancel_warning",
        help="La factura es de un pedido armado o entregado: anularla no lo desarma.")

    def _distri_packed_orders(self):
        self.ensure_one()
        if self.move_type != "out_invoice" or self.state == "cancel":
            return self.env["sale.order"]
        orders = self.sudo().invoice_line_ids.sale_line_ids.order_id
        return orders.filtered(lambda o: o._distri_packed_or_delivered())

    @api.depends("move_type", "state", "invoice_line_ids.sale_line_ids")
    def _compute_distri_cancel_warning(self):
        for move in self:
            move.distri_cancel_warning = bool(move.id and move._distri_packed_orders())

    def button_cancel(self):
        """19.0.1.129.26: after Odoo's cancel, the note and the to-do on
        each packed or delivered order of the cancelled invoices."""
        pending = []
        if not self.env.context.get(SKIP_CONTEXT):
            for move in self:
                orders = move._distri_packed_orders()
                if orders:
                    pending.append((move, move.name, orders))
        res = super().button_cancel()
        for move, name, orders in pending:
            if move.state == "cancel":
                orders._distri_notify_cancelled_invoice(name)
        return res
