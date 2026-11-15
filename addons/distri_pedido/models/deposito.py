"""Delivery at the depot and counter sales.

«Entrega en depósito»: the customer picks up a packed order at the depot.
The wizard asks the same questions as the driver's app (returns, receipt
code, cash, transfer, comment) and records them through the same code
(distri.sync.event._record_visit) on a stop of the user's depot "route" of
the day (distri.route with kind "depot"). So delivery states, returns and
pending receipts work exactly like on a route. The cash enters at once as
a customer payment in «Caja depósito», applied oldest invoice first; the
«Cierre de caja depósito» compares it with what the user counts.

«Venta en mostrador»: a quotation with a real customer is confirmed, packed
as ordered («armado rápido»: remito, invoice and receipt code as usual) and
delivered at the depot with the same wizard.
"""
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError

from .reparto import DEPOT_RECEIPT_ANSWERS, TRANSFER, _money

ADMIN_GROUP = "distri_pedido.grupo_administracion"

WITHOUT_CODE_REASON = "El cliente no tiene el código"
COUNTER_REASON = "Venta en mostrador (cliente presente)"


def _check_admin(env):
    if not (env.su or env.user.has_group(ADMIN_GROUP)):
        raise UserError("Solo administración y gerencia entregan en el depósito.")


class SaleOrderDeposito(models.Model):
    _inherit = "sale.order"

    def _distri_check_depot_delivery(self):
        """A packed, confirmed order of a real customer, not on a route."""
        self.ensure_one()
        if self.state != "sale":
            raise UserError("El pedido tiene que estar confirmado.")
        if self.distri_armado != "armado":
            raise UserError("El pedido todavía no está armado.")
        if self._distri_sin_cliente():
            raise UserError("Asigná el cliente al pedido antes de entregarlo.")
        if self.distri_delivery_state == "delivered":
            raise UserError("El pedido ya se entregó.")
        if self.distri_delivery_state == "dispatched":
            raise UserError("El pedido ya salió por encomienda.")
        busy = self.distri_stop_ids.filtered(
            lambda s: not (s.state == "not_delivered" and s.unloaded))
        if busy:
            raise UserError("El pedido está en la ruta %s. Sacalo de la ruta, o marcalo como "
                            "descargado en la rendición, antes de entregarlo en el depósito."
                            % busy[:1].route_id.name)

    def action_distri_depot_delivery(self):
        """«Entrega en depósito»: opens the wizard."""
        self.ensure_one()
        _check_admin(self.env)
        self._distri_check_depot_delivery()
        return {
            "type": "ir.actions.act_window",
            "name": "Venta en mostrador" if self.env.context.get("distri_counter_sale")
            else "Entrega en depósito",
            "res_model": "distri.depot.delivery",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_order_id": self.id,
                "default_counter_sale": bool(self.env.context.get("distri_counter_sale")),
            },
        }

    def action_distri_counter_sale(self):
        """«Venta en mostrador»: confirm, pack as ordered, deliver at the depot.

        The packing reuses the packer's own steps (distri_tomar and
        distri_terminar), so the remito is validated, the invoice comes out
        and the receipt code is generated exactly as after a normal packing.
        """
        self.ensure_one()
        _check_admin(self.env)
        if self.state not in ("draft", "sent"):
            raise UserError("La venta en mostrador sale de un presupuesto sin confirmar.")
        if self._distri_sin_cliente():
            raise UserError("Asigná el cliente real antes de la venta en mostrador.")
        if self.distri_armado != "pendiente":
            raise UserError("El pedido ya se tomó para armar.")
        if not self.order_line.filtered(lambda l: not l.display_type and l.product_uom_qty):
            raise UserError("El presupuesto no tiene productos.")
        self.action_confirm()
        error = self.distri_tomar() or self.distri_terminar(1)
        if error:
            raise UserError(error)
        if self.distri_factura_error:
            raise UserError("No salió la factura: %s" % self.distri_factura_error)
        self.message_post(body="Venta en mostrador: %s lo armó completo, tal como se pidió." %
                               self.env.user.name)
        return self.with_context(distri_counter_sale=True).action_distri_depot_delivery()


class DistriDepotDelivery(models.TransientModel):
    _name = "distri.depot.delivery"
    _description = "Entrega en depósito"

    order_id = fields.Many2one("sale.order", string="Pedido", required=True, readonly=True,
                               ondelete="cascade")
    counter_sale = fields.Boolean(string="Venta en mostrador", readonly=True)
    partner_id = fields.Many2one(related="order_id.partner_id", string="Cliente")
    currency_id = fields.Many2one(related="order_id.currency_id")
    parcels = fields.Integer(string="Bultos")
    order_line_ids = fields.One2many(related="order_id.order_line", string="Productos")

    # the same snapshot the phone gets (distri.route.stop._refresh_amounts)
    order_amount = fields.Monetary(string="Pedido de hoy", readonly=True)
    previous_debt = fields.Monetary(string="Deuda anterior", readonly=True)
    returns_amount = fields.Monetary(string="Devolución", compute="_compute_balance")
    expected_balance = fields.Monetary(string="Saldo total", compute="_compute_balance")

    returns_answer = fields.Selection([("none", "No, recibió todo"), ("yes", "Sí, devolvió")],
                                      string="¿Devolvió algo?")
    return_ids = fields.One2many("distri.depot.delivery.return", "wizard_id",
                                 string="Devoluciones")
    returns_received = fields.Boolean(
        string="Recibido en el depósito",
        help="Lo devuelto ya está en el depósito. Con esta marca sale la nota de crédito; "
             "sin ella, la devolución se controla en el «Cierre de caja depósito».")

    has_code = fields.Boolean(compute="_compute_has_code")
    code_option = fields.Selection([
        ("code", "Ingresar el código"),
        ("without_code", "El cliente no tiene el código · Entregar igual"),
    ], string="Código de recibido", required=True, default="code")
    counter_present = fields.Boolean(
        string=COUNTER_REASON,
        help="Venta en mostrador: el cliente está presente y no hace falta el código.")
    code = fields.Char(string="Código", size=4)

    cash = fields.Monetary(string="Efectivo")
    transfer = fields.Selection(TRANSFER, string="¿Paga con transferencia?", required=True,
                                default="no")
    transfer_amount = fields.Monetary(string="Importe transferido")
    receipt_answer = fields.Selection(DEPOT_RECEIPT_ANSWERS, string="Comprobante",
                                      help="El comprobante tiene que llegar al WhatsApp de la "
                                           "empresa. Queda pendiente hasta que llega.")
    no_payment = fields.Boolean(string="No pagó nada")
    comment = fields.Char(string="Comentario")

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        order = self.env["sale.order"].browse(values.get("order_id")).exists()
        if order:
            values.update({
                "parcels": order.distri_bultos,
                "order_amount": order._distri_amount_to_collect(),
                "previous_debt": order._distri_previous_debt(),
            })
            if values.get("counter_sale"):
                values["counter_present"] = True
        return values

    @api.depends("order_amount", "previous_debt", "return_ids.amount")
    def _compute_balance(self):
        for wizard in self:
            wizard.returns_amount = sum(wizard.return_ids.mapped("amount"))
            wizard.expected_balance = (wizard.order_amount + wizard.previous_debt
                                       - wizard.returns_amount)

    @api.depends("order_id")
    def _compute_has_code(self):
        for wizard in self:
            wizard.has_code = bool(wizard.order_id.sudo().distri_receipt_hash)

    @api.onchange("cash", "transfer")
    def _onchange_payment(self):
        if self.cash or self.transfer != "no":
            self.no_payment = False
        if self.transfer == "yes" and not self.transfer_amount:
            self.transfer_amount = max(self.order_amount - self.returns_amount - (self.cash or 0.0),
                                       0.0)

    # ------------------------------------------------------------ confirm

    def _visit_payload(self, stop):
        """The same payload the phone sends for a delivered visit."""
        self.ensure_one()
        payload = {
            "visit_uuid": str(uuid.uuid4()),
            "stop_id": stop.id,
            "outcome": "delivered",
            "returns_answer": self.returns_answer,
            "returns": [{
                "uuid": str(uuid.uuid4()),
                "line_id": ret.order_line_id.id,
                "qty": ret.quantity,
                "reason": ret.reason,
            } for ret in self.return_ids] if self.returns_answer == "yes" else [],
            "code": "",
            "without_code": False,
            "cash": self.cash or 0.0,
            "transfer": self.transfer or "no",
            "transfer_amount": self.transfer_amount if self.transfer == "yes" else 0.0,
            "receipt_answer": self.receipt_answer if self.transfer == "yes" else None,
            "no_payment": self.no_payment,
            "comment": self.comment or "",
        }
        if self._uses_code():
            payload["code"] = (self.code or "").strip()
        else:
            payload["without_code"] = True
        return payload

    def _uses_code(self):
        return self.code_option == "code" and not (self.counter_sale and self.counter_present)

    def _no_code_reason(self):
        if self._uses_code():
            return False
        return COUNTER_REASON if self.counter_sale and self.counter_present else WITHOUT_CODE_REASON

    def _check_answers(self):
        """What the app asks before «Guardar visita», with the same wording."""
        self.ensure_one()
        if not self.returns_answer:
            raise UserError("Falta responder si devolvió algo.")
        if self.returns_answer == "yes" and not self.return_ids:
            raise UserError("Cargá lo que devolvió.")
        if self.counter_present and not self.counter_sale:
            raise UserError("«%s» es solo para la venta en mostrador." % COUNTER_REASON)
        if self._uses_code():
            if not self.has_code:
                raise UserError("Este pedido no tiene código cargado. Usá «Entregar igual».")
            if not self.order_id._distri_check_receipt_code(self.code):
                raise UserError("El código no coincide. Pedíselo de nuevo al cliente, o elegí "
                                "«El cliente no tiene el código · Entregar igual».")
        if self.cash < 0:
            raise UserError("Importe inválido.")
        if self.no_payment and (self.cash or self.transfer != "no"):
            raise UserError("«No pagó nada» no lleva efectivo ni transferencia.")
        if not self.no_payment and not self.cash and self.transfer == "no":
            raise UserError("Si no pagó nada, marcá «No pagó nada».")
        if self.transfer == "yes" and not (self.transfer_amount and self.transfer_amount > 0):
            raise UserError("Poné el importe transferido.")
        if self.transfer == "yes" and not self.receipt_answer:
            raise UserError("Respondé si el comprobante ya llegó a la empresa.")

    def action_confirm(self):
        self.ensure_one()
        _check_admin(self.env)
        order = self.order_id
        order._distri_bloquear()
        order._distri_check_depot_delivery()
        self._check_answers()
        if self.parcels != order.distri_bultos:
            error = order.distri_corregir_bultos(self.parcels)
            if error:
                raise UserError(error)

        route = self.env["distri.route"]._depot_route_for(company=order.company_id)
        stop = route._add_orders(order)
        if not stop:
            raise UserError("El pedido ya está en las entregas en depósito de hoy.")
        payload = self._visit_payload(stop)
        self.env["distri.sync.event"].sudo()._record_visit(
            stop.sudo(), payload["visit_uuid"], payload, fields.Datetime.now())
        stop = stop.sudo(False)
        stop.invalidate_recordset()
        if not self._uses_code():
            stop.no_code_reason = self._no_code_reason()

        if stop.return_ids and self.returns_received:
            # the goods are here: the same user confirms them on the spot
            stop.return_ids.action_received()
            route._credit_received_returns()
        if stop.cash_amount > 0:
            route._register_stop_cash(stop)
        order.message_post(body=self._summary(stop))
        return {"type": "ir.actions.act_window_close"}

    def _summary(self, stop):
        parts = ["%s en el depósito (%s)." % (
            "Venta en mostrador entregada" if self.counter_sale else "Entregado",
            self.env.user.name)]
        if stop.delivery_state == "delivered_code":
            parts.append("Con código de recibido.")
        else:
            parts.append("Sin código de recibido: %s." % stop.no_code_reason)
        for ret in stop.return_ids:
            parts.append("Devolvió %g %s (%s): %s." % (
                ret.quantity, ret.product_id.display_name, ret.reason,
                dict(ret._fields["settlement_state"].selection)[ret.settlement_state]))
        if stop.cash_amount:
            parts.append("Efectivo %s en %s." % (
                _money(stop.cash_amount), stop.route_id.journal_id.name))
        if stop.transfer == "yes":
            parts.append("Transferencia de %s: comprobante pendiente hasta que llegue." % (
                _money(stop.transfer_amount)))
        if stop.state == "not_paid":
            parts.append("No pagó.")
        if stop.comment:
            parts.append("Comentario: %s" % stop.comment)
        return " ".join(parts)


class DistriDepotDeliveryReturn(models.TransientModel):
    _name = "distri.depot.delivery.return"
    _description = "Devolución en la entrega en depósito"

    wizard_id = fields.Many2one("distri.depot.delivery", required=True, ondelete="cascade")
    order_id = fields.Many2one(related="wizard_id.order_id")
    currency_id = fields.Many2one(related="wizard_id.currency_id")
    # the view restricts it to the lines of the wizard's order (parent.order_id)
    order_line_id = fields.Many2one("sale.order.line", string="Producto", required=True)
    quantity = fields.Float(string="Cantidad", digits="Product Unit", default=1.0)
    reason = fields.Char(string="Motivo", required=True)
    amount = fields.Monetary(string="Importe", compute="_compute_amount")

    @api.depends("order_line_id", "quantity")
    def _compute_amount(self):
        for ret in self:
            line = ret.order_line_id
            unit = (line.price_total / line.product_uom_qty) if line.product_uom_qty else 0.0
            ret.amount = unit * ret.quantity


class AccountJournalDeposito(models.Model):
    _inherit = "account.journal"

    @api.model
    def _distri_ensure_depot_journal(self):
        """Called on module update: «Caja depósito» in each company that
        already has its accounting set up."""
        for company in self.env["res.company"].sudo().search([]):
            if self.sudo().search_count([("company_id", "=", company.id),
                                         ("type", "=", "cash")]):
                self.env["distri.route"]._depot_journal(company)
