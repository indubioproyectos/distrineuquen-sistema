"""129.49 hotfix (owner, 26/11/2026): invoices and confirmed orders of the
packing circuit are not edited from the backend «por el momento».

The production audit of 26/11/2026 found invoices reset to draft, edited and
posted again (17 reopenings in 13 invoices, 4 with a different total) and
lines added to an order already delivered and invoiced (S00393): the invoice
then stops matching what left the depot, and the stock cannot be projected.

1. An invoice of the circuit is never reset to draft nor cancelled, by
   anyone: `button_draft`, `button_cancel` and a write of `state` refuse it
   (so the data API cannot go around the form), and the form hides «Reset
   to draft» (show_reset_to_draft_button). An invoice of the circuit is a
   customer invoice (out_invoice) with a goods line linked to a sale order
   line: everything that comes from Nueva venta, /pedido, AgilPedido, the
   end of the packing, Mostrador or Ya entregado. Not affected: «Facturación
   directa de servicios» (no sale order), vendor bills, credit notes, a
   draft invoice being cancelled (it never touched a balance), and code
   running as the superuser (odoo shell scripts, crons). The module's own
   flows that need it pass REABRIR_OK in the context: «Rearmar» is the only
   one (models/rearmar.py); «Anular venta» never resets an invoice, it
   reverses it with a credit note (models/anular_venta.py).

2. A confirmed order of the circuit (state «sale» with goods) does not get
   lines added, removed or changed through the backend: the order form, the
   product catalog and the data API (/web/dataset, /json/2, XML-RPC,
   JSON-RPC). The kit screens (/armado, /pedido, «Nueva venta», «Entrega en
   depósito», «Rearmar»'s copy, returns) are their own routes and keep
   working as before, as does code without an HTTP request (crons,
   scripts, the module's own methods). The module's own backend buttons
   that change lines pass LINEAS_OK, distri_armado or
   distri_agil_corrigiendo. The message replaces Odoo's «Once a sales order
   is confirmed, you can't remove one of its lines» with the buttons to use.

Nothing here writes data: it only refuses.
"""
from odoo import api, models
from odoo.exceptions import UserError
from odoo.http import request
from odoo.tools import float_compare

from .factura_directa import es_mercaderia

# the module's own flows that reset or cancel an invoice of the circuit
REABRIR_OK = "distri_factura_reabrir_ok"
# the module's own backend flows that change lines of a confirmed order
LINEAS_OK = "distri_lineas_permitidas"
LINEAS_OK_CONTEXTOS = (LINEAS_OK, "distri_armado", "distri_agil_corrigiendo")

# the backend's generic entry points: form saves, buttons, the data API
RUTAS_DEL_SISTEMA = ("/web/dataset/", "/json/2/", "/xmlrpc", "/jsonrpc", "/product/catalog/")

CAMPOS_LINEA = ("product_id", "product_uom_qty", "product_uom_id", "price_unit",
                "technical_price_unit", "discount", "tax_ids", "order_id")

FACTURA_BLOQUEADA = (
    "La factura %s no se puede reabrir ni cancelar: estamos ordenando las operaciones para "
    "proyectar el stock, y cada factura tiene que coincidir con lo que salió del depósito. "
    "Si el cliente devuelve algo, usá «Devolver» (en la visita o en la entrega en depósito). "
    "Si se agrega algo, hacé una venta nueva con «Agregar a este cliente». Para anular toda "
    "la venta, «Anular venta».")
LINEAS_BLOQUEADAS = (
    "El pedido %s ya está confirmado: no se le agregan, sacan ni cambian productos desde acá, "
    "porque tiene que coincidir con lo que sale del depósito y con su factura. Si todavía no "
    "se entregó y hay que corregirlo, usá «Rearmar». Si el cliente devuelve algo, usá "
    "«Devolver» (en la visita o en la entrega en depósito). Si se lleva algo más, hacé una "
    "venta nueva con «Agregar a este cliente». Para anular toda la venta, «Anular venta».")


def desde_el_sistema():
    """True while serving one of the backend's generic entry points (the
    order form, a button, the data API); False for the kit's own routes
    and without a request (crons, scripts)."""
    try:
        path = request.httprequest.path if request else ""
    except (AttributeError, RuntimeError):
        return False
    return bool(path) and path.startswith(RUTAS_DEL_SISTEMA)


def es_bien(product):
    """Goods: a product that is not a service."""
    return bool(product) and product.type != "service"


class AccountMoveBloqueo(models.Model):
    _inherit = "account.move"

    def _distri_factura_circuito(self):
        """A customer invoice with a goods line that comes from an order."""
        self.ensure_one()
        if self.move_type != "out_invoice":
            return False
        return any(line.sale_line_ids and es_mercaderia(line)
                   for line in self.sudo().invoice_line_ids)

    def _distri_reabrir_permitido(self):
        return self.env.su or bool(self.env.context.get(REABRIR_OK))

    def _distri_check_reabrir(self, moves):
        """Refuse when `moves` holds an invoice of the circuit."""
        if not moves or self._distri_reabrir_permitido():
            return
        for move in moves:
            if move._distri_factura_circuito():
                raise UserError(FACTURA_BLOQUEADA % (move.name or move.display_name))

    @api.depends_context("uid")
    def _compute_show_reset_to_draft_button(self):
        super()._compute_show_reset_to_draft_button()
        if self._distri_reabrir_permitido():
            return
        for move in self.filtered("show_reset_to_draft_button"):
            if move._distri_factura_circuito():
                move.show_reset_to_draft_button = False

    def button_draft(self):
        self._distri_check_reabrir(self.filtered(lambda m: m.state in ("posted", "cancel")))
        return super().button_draft()

    def button_cancel(self):
        # a draft invoice is cancelled as always (it never reached a balance)
        self._distri_check_reabrir(self.filtered(lambda m: m.state in ("posted", "cancel")))
        return super().button_cancel()

    def write(self, vals):
        if "state" in vals and not self._distri_reabrir_permitido():
            new = vals["state"]
            self._distri_check_reabrir(self.filtered(
                lambda m: (m.state == "posted" and new != "posted")
                or (m.state == "cancel" and new != "cancel")))
        return super().write(vals)


class SaleOrderLineBloqueo(models.Model):
    _inherit = "sale.order.line"

    @api.model
    def _distri_lineas_libres(self):
        env = self.env
        return env.su or any(env.context.get(key) for key in LINEAS_OK_CONTEXTOS) \
            or not desde_el_sistema()

    @staticmethod
    def _distri_pedido_circuito(order, extra_product=None):
        """Confirmed with goods (sudo)."""
        order = order.sudo()
        if order.state != "sale":
            return False
        if es_bien(extra_product):
            return True
        return any(es_bien(line.product_id)
                   for line in order.order_line if not line.display_type)

    def _distri_refuse(self, order):
        raise UserError(LINEAS_BLOQUEADAS % order.sudo().name)

    @api.model_create_multi
    def create(self, vals_list):
        if not self._distri_lineas_libres():
            Order, Product = self.env["sale.order"], self.env["product.product"]
            for vals in vals_list:
                if not vals.get("order_id") or vals.get("display_type"):
                    continue
                order = Order.browse(vals["order_id"])
                product = Product.browse(vals.get("product_id") or []).sudo()
                if self._distri_pedido_circuito(order, product):
                    self._distri_refuse(order)
        return super().create(vals_list)

    def _distri_cambia(self, vals):
        """The lines of a confirmed order of the circuit whose product,
        quantity, price, unit, discount, taxes or order `vals` changes."""
        campos = [f for f in CAMPOS_LINEA if f in vals]
        if not campos:
            return self.browse()
        changed = self.browse()
        for line in self.sudo().filtered(lambda l: not l.display_type):
            for field in campos:
                value = vals[field]
                current = line[field]
                if self._fields[field].type in ("many2one",):
                    if (current.id or False) != (value or False):
                        changed |= line
                        break
                elif self._fields[field].type in ("float", "monetary"):
                    if float_compare(current or 0.0, value or 0.0, precision_digits=6):
                        changed |= line
                        break
                else:
                    changed |= line
                    break
        return changed.filtered(lambda l: self._distri_pedido_circuito(l.order_id))

    def write(self, vals):
        if not self._distri_lineas_libres():
            changed = self._distri_cambia(vals)
            if changed:
                self._distri_refuse(changed[:1].order_id)
            if vals.get("order_id"):
                order = self.env["sale.order"].browse(vals["order_id"])
                if self._distri_pedido_circuito(order):
                    self._distri_refuse(order)
        return super().write(vals)

    def unlink(self):
        if not self._distri_lineas_libres():
            for line in self.sudo().filtered(lambda l: not l.display_type):
                if self._distri_pedido_circuito(line.order_id):
                    self._distri_refuse(line.order_id)
        return super().unlink()
