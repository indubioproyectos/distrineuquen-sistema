"""19.0.1.129.35: «Anular venta» in one step (#97, second part; plan
unificado, Etapa 4). Behind distri_pedido.nueva_venta (the kit screen is
/pedidos/<id>/anular, controllers/anular_venta.py, which also needs
distri_pedido.ui_v2). Administración only (gerencia implies it).

Until now cancelling a sale took three manual steps (the invoice, the return
of the remito, the order) and on 24/11/2026 PAINEN BELEN's S00212 was fixed
by script (scripts/correcciones/painen_cancelar_s00212_body.py). This does
the same in one step, with Odoo's own flows only:

- every posted customer invoice of the order gets a credit note by Odoo's
  standard reversal: the values of the «Revertir» wizard
  (account.move.reversal._prepare_default_reversal) and
  account.move._reverse_moves(cancel=True), the same call the wizard makes
  for «Revertir y crear factura». The credit note is posted and reconciled
  with the invoice, so the invoice is «Revertida» and its residual is 0.
  The posted invoice is never reset to draft, edited or deleted. For the
  customer's balance this is exactly the old manual path (the invoice
  cancelled): it goes down by the invoice's residual;
- every validated delivery (remito) gets Odoo's return (stock.return.picking,
  the DEV-… «Devoluciones de clientes» type of models/devolucion.py) of what
  is still out, validated at once: the goods are back in stock;
- the order is cancelled by sale's own _action_cancel, which cancels its
  draft invoices and its pending deliveries.

It refuses, and does nothing, when money or a route is involved: an invoice
that also bills another order, an invoice with payments or anything reconciled (a receipt, a credit note of a return),
a credit note already made, an approved Mercado Pago payment, a stop on a
route not settled yet, a dispatched parcel, or the order being packed right
now. Nothing is ever unreconciled here: _reverse_moves(cancel=True) would
unreconcile the invoice's lines first, and the refusal makes sure there is
nothing to unreconcile.

The screen shows the plan (`_distri_anular_plan`, the same list this code
runs) and the reason is mandatory; it goes to the chatter of the order, the
invoice and the customer.
"""
import hashlib

from markupsafe import Markup

from odoo import fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare, float_is_zero

from .nueva_venta import nueva_venta_on
from .rearmar import _has_payments
from .reparto import _money

ANULAR_GROUP = "distri_pedido.grupo_administracion"
NOT_ALLOWED = "Solo administración y gerencia anulan una venta."
FLAG_OFF = "«Anular venta» no está activo."
NO_REASON = "Escribí el motivo de la anulación."
CHANGED = ("La venta cambió mientras mirabas lo que se iba a hacer. Revisá la lista de nuevo "
           "y confirmá otra vez.")
MOTIVO_MAX = 250


def puede_anular(user):
    """Administración (gerencia implies it). Sistemas alone is not a sales role."""
    return bool(user) and not user.share and user.has_group(ANULAR_GROUP)


class SaleOrderAnularVenta(models.Model):
    _inherit = "sale.order"

    distri_anulada_motivo = fields.Char(
        string="Motivo de la anulación", readonly=True, copy=False,
        help="«Anular venta» (19.0.1.129.35): por qué se anuló la venta.")
    distri_anulada_por_id = fields.Many2one(
        "res.users", string="Anuló la venta", readonly=True, copy=False)
    distri_anulada_el = fields.Datetime(string="Venta anulada el", readonly=True, copy=False)

    # ------------------------------------------------------------ the plan

    def _distri_anular_devoluciones(self):
        """[(remito, [(move, quantity in the move's unit)])]: what of each
        validated delivery is still out (sent minus what already came back)."""
        self.ensure_one()
        result = []
        pickings = self.sudo().picking_ids.filtered(
            lambda p: p.state == "done" and p.picking_type_code == "outgoing")
        for picking in pickings.sorted("id"):
            moves = []
            for move in picking.move_ids.filtered(
                    lambda m: m.state == "done" and not m.origin_returned_move_id):
                back = sum(m.product_uom._compute_quantity(m.quantity, move.product_uom)
                           for m in move.returned_move_ids if m.state != "cancel")
                left = move.quantity - back
                if float_compare(left, 0.0, precision_rounding=move.product_uom.rounding) > 0:
                    moves.append((move, left))
            if moves:
                result.append((picking, moves))
        return result

    def _distri_anular_plan(self):
        """What «Anular venta» would do now, for the screen and for the run:
        {"refusals": [text], "steps": [text], "invoices": posted invoices to
        reverse, "drafts": draft invoices, "returns": _distri_anular_
        devoluciones(), "baja": what the customer's balance goes down,
        "huella": a fingerprint of the steps}."""
        self.ensure_one()
        order = self.sudo()
        refusals, steps = [], []
        invoices = order.invoice_ids.filtered(lambda m: m.move_type == "out_invoice")
        posted = invoices.filtered(lambda m: m.state == "posted")
        drafts = invoices.filtered(lambda m: m.state == "draft")
        refunds = order.invoice_ids.filtered(
            lambda m: m.move_type == "out_refund" and m.state != "cancel")

        if order.state == "cancel":
            refusals.append("La venta ya está anulada.")
        if order.distri_armado == "en_armado":
            refusals.append("%s lo está armando ahora: esperá que termine o que lo deje para "
                            "más tarde." % (order.distri_armador_id.name or "Un armador"))
        for invoice in invoices:
            others = invoice.invoice_line_ids.sale_line_ids.order_id - order
            if others:
                refusals.append("La factura %s también es de %s: anulá a mano desde el sistema "
                                "lo que corresponda." % (invoice.name or "en borrador",
                                                         ", ".join(others.mapped("name"))))
        for invoice in posted:
            name = invoice.name or invoice.display_name
            if invoice.reversal_move_ids.filtered(lambda r: r.state != "cancel"):
                refusals.append("La factura %s ya tiene una nota de crédito: anulá la venta "
                                "a mano desde el sistema." % name)
            elif _has_payments(invoice):
                refusals.append("La factura %s tiene pagos o notas de crédito aplicadas. "
                                "Desde acá no se desconcilia nada: primero hay que quitarlos "
                                "en el sistema." % name)
        if refunds and not refusals:
            refusals.append("El pedido ya tiene una nota de crédito (%s): anulá la venta a mano "
                            "desde el sistema." % ", ".join(r.name or "borrador" for r in refunds))
        if order._distri_mp_approved_payments():
            refusals.append("El pedido tiene un pago aprobado de Mercado Pago: la plata del "
                            "cliente no se toca desde acá.")
        open_stops = order.distri_stop_ids.filtered(lambda s: s.route_id.state != "settled")
        for stop in open_stops[:1]:
            refusals.append("El pedido está en %s, que todavía no se rindió: sacalo de la ruta "
                            "o esperá la rendición." % stop.route_id.display_name)
        if order.distri_parcel_ids:
            refusals.append("El pedido ya salió por encomienda: anulalo a mano cuando vuelva "
                            "la mercadería.")

        returns = order._distri_anular_devoluciones() if order.state != "cancel" else []
        for invoice in posted:
            steps.append("Se emite una nota de crédito por %s que anula la factura %s "
                         "(la factura no se borra ni se cambia)." % (
                             _money(invoice.amount_residual), invoice.name))
        for invoice in drafts:
            steps.append("Se anula la factura en borrador (nunca se emitió).")
        for picking, moves in returns:
            steps.append("Vuelve al stock lo del remito %s: %s." % (picking.name, ", ".join(
                "%g %s" % (qty, move.product_id.display_name) for move, qty in moves)))
        pending = order.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        for picking in pending:
            steps.append("Se cancela el remito pendiente %s." % picking.name)
        if order.state in ("draft", "sent"):
            steps.append("Se cancela el presupuesto %s." % order.name)
        elif order.state == "sale":
            steps.append("Se cancela el pedido %s: sale de Armado, del Depósito y de la "
                         "planificación de rutas." % order.name)
        baja = sum(posted.mapped("amount_residual"))
        if posted:
            steps.append("El saldo del cliente baja %s." % _money(baja))
        steps.append("No se toca ningún cobro, pago ni conciliación anterior.")
        huella = hashlib.sha256("\n".join(steps).encode()).hexdigest()[:20]
        return {"refusals": refusals, "steps": steps, "invoices": posted, "drafts": drafts,
                "returns": returns, "baja": baja, "huella": huella}

    # ------------------------------------------------------------ the run

    def distri_anular_venta(self, motivo, huella=""):
        """«Anular venta»: the plan above, in one transaction. `huella` (the
        plan the user saw) must still be the plan now. Returns the plan run."""
        self.ensure_one()
        if not nueva_venta_on(self.env):
            raise UserError(FLAG_OFF)
        if not (self.env.su or puede_anular(self.env.user)):
            raise AccessError(NOT_ALLOWED)
        motivo = " ".join((motivo or "").split())[:MOTIVO_MAX]
        if len(motivo) < 3:
            raise UserError(NO_REASON)
        order = self.sudo()
        # the packer's and the customer's lock: nobody takes it meanwhile
        order._distri_bloquear()
        plan = order._distri_anular_plan()
        if plan["refusals"]:
            raise UserError(plan["refusals"][0])
        if huella and huella != plan["huella"]:
            raise UserError(CHANGED)
        user = self.env.user
        refunds = self.env["account.move"]
        for invoice in plan["invoices"]:
            refunds |= order._distri_anular_factura(invoice, motivo)
        returned = self.env["stock.picking"]
        for picking, moves in plan["returns"]:
            returned |= order._distri_anular_remito(picking, moves)
        # sale's own cancel: its draft invoices and pending deliveries too;
        # on purpose, so no «factura anulada» note (models/factura_anulada.py)
        order.with_context(distri_skip_cancel_notice=True)._action_cancel()
        if order.state != "cancel":
            raise UserError("No se pudo cancelar el pedido %s." % order.name)
        order.write({"distri_anulada_motivo": motivo, "distri_anulada_por_id": user.id,
                     "distri_anulada_el": fields.Datetime.now()})
        body = Markup("<b>Venta anulada</b> por %s. Motivo: %s.<br/>%s") % (
            user.name, motivo, Markup("<br/>").join(plan["steps"]))
        if refunds:
            body += Markup("<br/>Notas de crédito: %s.") % Markup(", ").join(
                r._get_html_link() for r in refunds)
        if returned:
            body += Markup("<br/>Devoluciones al stock: %s.") % Markup(", ").join(
                p._get_html_link() for p in returned)
        order.message_post(body=body)
        partner = order.partner_id.commercial_partner_id
        if partner and not partner.distri_generic_partner:
            partner.message_post(body=Markup("Venta %s anulada por %s. Motivo: %s.") % (
                order._get_html_link(), user.name, motivo))
        plan.update(refunds=refunds, returned=returned)
        return plan

    def _distri_anular_factura(self, invoice, motivo):
        """A credit note of `invoice` by Odoo's standard reversal, posted
        and reconciled with it (the invoice itself is not touched)."""
        self.ensure_one()
        wizard = self.env["account.move.reversal"].sudo().with_context(
            active_model="account.move", active_ids=invoice.ids).create({
                "move_ids": [(6, 0, invoice.ids)],
                "reason": "Anular venta %s: %s" % (self.name, motivo),
                "journal_id": invoice.journal_id.id,
            })
        values = wizard._prepare_default_reversal(invoice)
        # posted now, never scheduled (the wizard posts later only a future date)
        values["auto_post"] = "no"
        refund = invoice._reverse_moves([values], cancel=True)
        invoice.invalidate_recordset(["payment_state", "amount_residual"])
        if refund.state != "posted" or not float_is_zero(
                invoice.amount_residual, precision_rounding=invoice.currency_id.rounding):
            raise UserError("La nota de crédito de %s no quedó aplicada a la factura: no se "
                            "anuló nada." % invoice.name)
        invoice.message_post(body=Markup("Anulada por «Anular venta» con la nota de crédito %s "
                                         "(%s). Motivo: %s.") % (
            refund._get_html_link(), self.env.user.name, motivo))
        return refund

    def _distri_anular_remito(self, picking, moves):
        """Odoo's return of `moves` [(move, quantity)] of the validated
        delivery `picking`, validated at once."""
        self.ensure_one()
        warehouse = picking.picking_type_id.warehouse_id
        if warehouse:
            self.env["stock.picking.type"]._distri_return_type(warehouse)
        quantities = dict((move.id, qty) for move, qty in moves)
        wizard = self.env["stock.return.picking"].sudo().with_context(
            active_id=picking.id, active_model="stock.picking",
            active_ids=picking.ids).create({"picking_id": picking.id})
        for line in wizard.product_return_moves:
            line.quantity = quantities.get(line.move_id.id, 0.0)
        ret = wizard._create_return()
        ret.origin = "Devolución de %s · venta anulada %s" % (picking.name, self.name)
        for move in ret.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        result = ret.with_context(skip_backorder=True, skip_sms=True,
                                  picking_ids_not_to_backorder=ret.ids).button_validate()
        if ret.state != "done":
            raise UserError("No se pudo validar la devolución al stock %s (%s)." % (
                ret.name, isinstance(result, dict) and result.get("name") or ""))
        return ret
