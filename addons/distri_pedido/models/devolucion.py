"""Returns put the goods back in stock and are never invoiced again.

Decision of ale (27/09/2026): a return counts for the customer as soon as
the driver reports it. From the moment the visit syncs (or the depot
delivery is saved) the account shows a «Devolución informada» line marked
«a controlar» and the balance already subtracts it
(_distri_is_provisional, models/estado_cuenta.py). The credit note matters
only when the driver settles the returns:

- «Recibido en el depósito»: posted credit note plus the stock return below;
- «No volvió»: see reparto.MISSING_RETURN_CREDITS_CUSTOMER.

Either way the posted credit note replaces the provisional line, so the
amount never counts twice.

A return declared on a route stop or at a depot delivery is settled once the
administrator marks it «Recibido en el depósito» (the route settlement, the
«Cierre de caja depósito», or the depot delivery wizard itself). At that
point, besides the credit note (reparto._credit_received_returns):

- a stock return of those quantities is created against the order's
  validated delivery (the remito R-…) and validated at once. Its moves are
  linked to the sale lines, so qty_delivered goes down. The return has its
  own operation type «Devoluciones de clientes» with the DEV-NNNNN sequence;
  the customer still sees only the remito and the «Devolución» line
  (remito._distri_remitos lists outgoing deliveries only);
- the returned quantity stops being invoiceable. The products are invoiced
  on ordered quantities (invoice_policy "order"), where Odoo would offer to
  invoice the returned units again after the credit note. The sale line
  subtracts the settled returns from qty_to_invoice, counts them as invoiced
  for the invoice status, and _create_invoices refuses an invoice that
  would bill them.
"""
from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero

RETURN_TYPE_NAME = "Devoluciones de clientes"
RETURN_TYPE_CODE = "DEV"
RETURN_PREFIX = "DEV-"


class StockPickingTypeDevolucion(models.Model):
    _inherit = "stock.picking.type"

    @api.model
    def _distri_return_type(self, warehouse):
        """«Devoluciones de clientes» of the warehouse, created the first time.

        It becomes the return type of the warehouse's delivery type, so a
        return of a remito R-… is named DEV-00001 and never mixes with the
        purchase receipts (WH/IN/…).
        """
        Type = self.sudo().with_context(active_test=False)
        return_type = Type.search([("warehouse_id", "=", warehouse.id),
                                   ("sequence_code", "=", RETURN_TYPE_CODE),
                                   ("code", "=", "incoming")], limit=1)
        if not return_type:
            sequence = self.env["ir.sequence"].sudo().create({
                "name": "%s %s" % (warehouse.name, RETURN_TYPE_NAME),
                "prefix": RETURN_PREFIX,
                "padding": 5,
                "company_id": warehouse.company_id.id,
            })
            return_type = Type.create({
                "name": RETURN_TYPE_NAME,
                "code": "incoming",
                "sequence_code": RETURN_TYPE_CODE,
                "sequence_id": sequence.id,
                "warehouse_id": warehouse.id,
                "company_id": warehouse.company_id.id,
                "default_location_src_id": self.env.ref("stock.stock_location_customers").id,
                "default_location_dest_id": warehouse.lot_stock_id.id,
            })
        elif not return_type.active:
            return_type.active = True
        deliveries = Type.search([("warehouse_id", "=", warehouse.id),
                                  ("code", "=", "outgoing")])
        deliveries.filtered(lambda t: t.return_picking_type_id != return_type).write(
            {"return_picking_type_id": return_type.id})
        if warehouse.out_type_id and return_type.return_picking_type_id != warehouse.out_type_id:
            return_type.return_picking_type_id = warehouse.out_type_id
        return return_type

    @api.model
    def _distri_ensure_return_types(self):
        """Called on module update: one per warehouse."""
        for warehouse in self.env["stock.warehouse"].sudo().search([]):
            self._distri_return_type(warehouse)


class DistriRouteReturnStock(models.Model):
    _inherit = "distri.route.return"

    processed = fields.Boolean(
        string="Aplicada", readonly=True, copy=False,
        help="Rendida y aplicada: nota de crédito emitida y, si volvió al depósito, "
             "devolución al stock. Desde ahí la cantidad no se vuelve a facturar.")
    return_picking_id = fields.Many2one("stock.picking", string="Devolución al stock",
                                        readonly=True, copy=False)
    provisional = fields.Boolean(
        string="A controlar", compute="_compute_provisional",
        help="Informada por el repartidor y todavía sin rendir: ya figura en la cuenta "
             "del cliente como «Devolución informada».")

    def _compute_provisional(self):
        for ret in self:
            ret.provisional = ret._distri_is_provisional()

    def _distri_is_provisional(self):
        """Reported but not settled: it already lowers the customer's balance.

        Only when the goods were billed (the order has a posted invoice):
        otherwise the invoice itself will leave the returned units out.
        Once a posted credit note exists the accounting carries it. A draft
        credit note (made before 27/09/2026) still counts as provisional.
        """
        self.ensure_one()
        ret = self.sudo()
        if ret.credit_note_id:
            return ret.credit_note_id.state == "draft"
        if ret.processed or ret.route_id.state == "settled":
            return False
        return bool(ret.order_id._distri_facturas_emitidas())

    @api.model
    def _distri_provisional_for(self, partner):
        """Provisional returns of a customer (commercial partner)."""
        partner = partner.commercial_partner_id
        if not partner:
            return self.browse()
        returns = self.sudo().search([
            ("order_id.partner_id.commercial_partner_id", "=", partner.id),
            ("quantity", ">", 0),
        ])
        return returns.filtered(lambda r: r._distri_is_provisional())

    @api.model
    def _distri_provisional_for_order(self, order):
        returns = self.sudo().search([("order_id", "=", order.id), ("quantity", ">", 0)])
        return returns.filtered(lambda r: r._distri_is_provisional())


class SaleOrderLineDevolucion(models.Model):
    _inherit = "sale.order.line"

    distri_return_ids = fields.One2many("distri.route.return", "order_line_id",
                                        string="Devoluciones")
    distri_qty_returned = fields.Float(
        string="Devuelto", digits="Product Unit", compute="_compute_distri_qty_returned",
        store=True,
        help="Lo que el cliente devolvió y se recibió en el depósito. No se vuelve a facturar.")

    @api.depends("distri_return_ids.quantity", "distri_return_ids.processed")
    def _compute_distri_qty_returned(self):
        for line in self:
            line.distri_qty_returned = sum(
                line.distri_return_ids.filtered("processed").mapped("quantity"))

    @api.depends("distri_qty_returned", "distri_return_ids.return_picking_id")
    def _compute_qty_to_invoice(self):
        super()._compute_qty_to_invoice()
        for line in self:
            returned = line.distri_qty_returned
            # on delivered quantities the stock return already lowered
            # qty_delivered, except for returns that never came back to the
            # depot («No volvió»); on ordered quantities every settled return
            # is taken off here
            if returned and line.product_id.invoice_policy != "order":
                returned = sum(line.distri_return_ids.filtered(
                    lambda r: r.processed and not r.return_picking_id).mapped("quantity"))
            if not returned:
                continue
            if line.qty_to_invoice > 0:
                line.qty_to_invoice = max(0.0, line.qty_to_invoice - returned)

    @api.depends("distri_qty_returned")
    def _compute_invoice_status(self):
        super()._compute_invoice_status()
        precision = self.env["decimal.precision"].precision_get("Product Unit")
        for line in self:
            if (line.invoice_status == "no" and line.state == "sale"
                    and line.distri_qty_returned
                    and float_is_zero(line.qty_to_invoice, precision_digits=precision)
                    and float_compare(line.qty_invoiced + line.distri_qty_returned,
                                      line.product_uom_qty, precision_digits=precision) >= 0):
                line.invoice_status = "invoiced"

    def _distri_invoiceable_limit(self):
        """Most that can be invoiced on the line: ordered minus returned."""
        self.ensure_one()
        return self.product_uom_qty - self.distri_qty_returned


class SaleOrderDevolucion(models.Model):
    _inherit = "sale.order"

    def _create_invoices(self, grouped=False, final=False, date=None):
        """Never invoice what the customer returned."""
        moves = super()._create_invoices(grouped=grouped, final=final, date=date)
        lines = self.order_line.filtered("distri_qty_returned")
        if lines:
            lines.invalidate_recordset(["qty_invoiced"])
            precision = self.env["decimal.precision"].precision_get("Product Unit")
            over = lines.filtered(lambda l: float_compare(
                l.qty_invoiced, l._distri_invoiceable_limit(), precision_digits=precision) > 0)
            if over:
                raise UserError(
                    "No se factura lo que el cliente devolvió: %s." % ", ".join(
                        "%s (%s, devolvió %g)" % (l.product_id.display_name, l.order_id.name,
                                                  l.distri_qty_returned) for l in over))
        return moves

    def _distri_return_stock(self, returns, label):
        """Stock return of the received returns against the validated delivery.

        One return picking per delivery, validated right away. Returns
        without a validated delivery behind them (the order was never packed
        through the depot) are only noted on the order. Returns the pickings.
        """
        self.ensure_one()
        order = self.sudo()
        per_move = defaultdict(float)
        deliveries_of = defaultdict(set)
        unmatched = []
        for ret in returns:
            line = ret.order_line_id
            left = ret.quantity
            moves = line.move_ids.filtered(
                lambda m: m.state == "done" and m.picking_code == "outgoing"
                and not m.origin_returned_move_id).sorted("id")
            for move in moves:
                if float_is_zero(left, precision_rounding=line.product_uom_id.rounding):
                    break
                sent = move.product_uom._compute_quantity(move.quantity, line.product_uom_id)
                back = sum(m.product_uom._compute_quantity(m.quantity, line.product_uom_id)
                           for m in move.returned_move_ids if m.state != "cancel")
                take = min(left, sent - back - per_move[move.id])
                if take > 0:
                    per_move[move.id] += take
                    deliveries_of[ret.id].add(move.picking_id.id)
                    left -= take
            if left > 0 and not float_is_zero(left, precision_rounding=line.product_uom_id.rounding):
                unmatched.append("%g %s" % (left, line.product_id.display_name))
        if unmatched:
            order.message_post(body="Devolución recibida (%s) sin entrega validada para "
                                    "volver al stock: %s." % (label, ", ".join(unmatched)))

        Move = self.env["stock.move"].sudo()
        pickings = self.env["stock.picking"]
        created = {}
        by_picking = defaultdict(dict)
        for move_id, qty in per_move.items():
            move = Move.browse(move_id)
            line = move.sale_line_id
            by_picking[move.picking_id][move] = line.product_uom_id._compute_quantity(
                qty, move.product_uom)
        for delivery, quantities in by_picking.items():
            warehouse = delivery.picking_type_id.warehouse_id
            if warehouse:
                self.env["stock.picking.type"]._distri_return_type(warehouse)
            wizard = self.env["stock.return.picking"].sudo().with_context(
                active_id=delivery.id, active_model="stock.picking",
                active_ids=delivery.ids).create({"picking_id": delivery.id})
            for wiz_line in wizard.product_return_moves:
                wiz_line.quantity = quantities.get(wiz_line.move_id, 0.0)
            picking = wizard._create_return()
            picking.origin = "Devolución de %s · %s" % (delivery.name, label)
            for move in picking.move_ids:
                move.quantity = move.product_uom_qty
                move.picked = True
            result = picking.with_context(skip_backorder=True, skip_sms=True,
                                          picking_ids_not_to_backorder=picking.ids).button_validate()
            if picking.state != "done":
                raise UserError("No se pudo validar la devolución al stock %s (%s)." % (
                    picking.name, isinstance(result, dict) and result.get("name") or ""))
            created[delivery.id] = picking
            pickings |= picking
        for ret in returns:
            ids = sorted(deliveries_of.get(ret.id, ()))
            if ids and ids[0] in created:
                ret.return_picking_id = created[ids[0]]
        return pickings
