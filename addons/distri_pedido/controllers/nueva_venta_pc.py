"""«Nueva venta» on the PC, «estilo planilla» (19.0.1.129.53, owner 26/11/2026:
«en una pantalla cerrar toda la operación»). Behind distri_pedido.nueva_venta
and distri_pedido.ui_v2: with either off every route answers 404 and
/nueva-venta never switches to it.

    /nueva-venta
      /nueva-venta/planilla                 GET   the desktop screen: customer,
                                                  lines table, exit, and after
                                                  «Cargar la venta» the delivery,
                                                  the payment and the summary
        /nueva-venta/planilla/venta/<order> GET   JSON: what was done with the
                                                  sale and what can follow
        /nueva-venta/planilla/cobrar/<order> POST JSON: «Cobrar» of that sale

Nothing here is a parallel implementation. The screen
(static/src/nueva_venta/planilla.js) calls the endpoints the phone and the
depot already use, by fetch:
- the customer search: /pedido/clientes; his priced catalog:
  /nueva-venta/catalogo (his list, hidden lists, lista 3/4, «a presupuestar»);
  «Nuevo cliente»: /nueva-venta/cliente;
- the sale: /nueva-venta/crear (the 4 exits, the same validations); its JSON
  answer carries the order id since this version;
- the delivery of «Mostrador» with its payment (cash to «Caja depósito»,
  transfer with «Subir comprobante» / «Lo manda después», returns):
  POST /deposito/entrega/<order>?mostrador=1, the depot form's own code
  (wizard distri.depot.delivery);
- «Cobrar» (e.g. «Ya entregado», or what a counter sale left owing): the
  route below, which calls distri.cobro.collect_receipts / collect exactly as
  /cobro/registrar does (same cash box rules, same receipt crediting).
The summary only reads.
"""
from werkzeug.exceptions import NotFound

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models import nueva_venta as nv
from ..models.cobrar import CREDIT_OK, MAX_TRANSFERS, QUESTIONS, can_collect, question_field
from ..models.cobro_efectivo import RouteCashWarning
from ..models.comprobante_subir import MAX_FILE_BYTES
from ..models.reparto import _money
from .kit import ui_v2_on
from .nueva_venta import NuevaVenta

TEMPLATE = "distri_pedido.nueva_venta_planilla"
REMITO_PDF = "/report/pdf/distri_pedido.remito_pedido_documento/%s"


def planilla_on(env):
    """The PC layout: «Nueva venta» and the design system both on."""
    return nv.nueva_venta_on(env) and ui_v2_on(env)


class NuevaVentaPlanilla(http.Controller):
    """Uses «Nueva venta»'s own helpers (an instance of its controller, as
    it uses /pedido's) instead of copying them."""

    def _nv(self):
        return NuevaVenta()

    def _json(self, data, status=200):
        return self._nv()._json(data, status=status)

    def _check_pc(self):
        user = self._nv()._check()
        if not ui_v2_on(request.env):
            raise NotFound()
        return user

    # ---------------------------------------------------------------- screen

    @http.route("/nueva-venta/planilla", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def planilla(self, **kw):
        user = self._check_pc()
        from .kit import kit_context
        base = self._nv()
        return base._render(TEMPLATE, {
            "dn": kit_context("planilla.css", "planilla.js"),
            "salidas": [s for s in nv.SALIDAS if s[0] in nv.salidas_de(user)],
            "uid": user.id,
            "wa_requerido": base._wa_requerido(),
            "localidades": base._pedido()._localidades_encomienda(),
            "motivo": nv.MOTIVO_ENTREGADO,
            "max_transf": MAX_TRANSFERS,
        })

    # ---------------------------------------------------------------- the sale

    def _venta(self, order_id):
        """The order as «/nueva-venta/hecho» reads it: 404 for one the user
        cannot read or reach."""
        order = request.env["sale.order"].browse(int(order_id)).exists()
        if not order:
            raise NotFound()
        try:
            order.check_access("read")
        except AccessError:
            raise NotFound() from None
        if not request.env.user._distri_may_reach_record(order):
            raise NotFound()
        return order

    @http.route("/nueva-venta/planilla/venta/<int:order_id>", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def venta(self, order_id, **kw):
        self._check_pc()
        return self._json(dict(self._resumen(self._venta(order_id)), ok=True))

    def _facturas(self, o):
        return o.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted")

    def _entrega(self, order):
        """The depot delivery form's data while the user may deliver it now
        (the backend button's own checks, nothing written), else None."""
        from .deposito import is_depot
        user = request.env.user
        if not is_depot(user):
            return None
        try:
            order._distri_check_depot_delivery()
        except UserError:
            return None
        Wizard = request.env["distri.depot.delivery"].with_context(
            default_order_id=order.id, default_counter_sale=True)
        values = Wizard.default_get(["order_id", "counter_sale", "parcels", "order_amount",
                                     "previous_debt", "counter_present"])
        o = order.sudo()
        lines = order.order_line.filtered(lambda l: not l.display_type and l.product_uom_qty > 0)
        return {
            "pedido": values.get("order_amount") or 0.0,
            "deuda": values.get("previous_debt") or 0.0,
            "bultos": values.get("parcels") or 0,
            "tiene_codigo": bool(o.distri_receipt_hash),
            "lineas": [{
                "id": l.id, "nombre": l.product_id.display_name, "cantidad": l.product_uom_qty,
                "unidad": l.product_uom_id.name or "",
                "unitario": (l.price_total / l.product_uom_qty) if l.product_uom_qty else 0.0,
            } for l in lines],
        }

    def _pagos(self, o, facturas):
        """What was collected for this sale, read only: the depot visit
        (cash, transfer) and the one-step collections on its invoices."""
        rows = []
        stops = o.distri_stop_ids.filtered(
            lambda s: s.route_id.kind == "depot" and s.state != "pending")
        for stop in stops:
            if stop.cash_amount:
                rows.append("Efectivo %s en %s" % (_money(stop.cash_amount),
                                                    stop.route_id.journal_id.name or "la caja"))
            if stop.transfer == "yes":
                rows.append("Transferencia %s: comprobante pendiente hasta que se verifique"
                            % _money(stop.transfer_amount))
            if stop.state == "not_paid":
                rows.append("En la entrega no pagó")
        if facturas:
            cobros = request.env["distri.cobro"].sudo().search(
                [("invoice_id", "in", facturas.ids)], order="id")
            for cobro in cobros:
                rows.append("Cobro %s: %s" % (_money(cobro.amount), cobro.memo or ""))
        return rows

    def _resumen(self, order):
        """What the summary shows: the documents (to open in a new tab), the
        payments, and the steps still possible on this screen."""
        o = order.sudo()
        titulo, texto = NuevaVenta._titulo_texto(o)
        facturas = self._facturas(o)
        debe = round(sum(facturas.mapped("amount_residual")), 2)
        entregado = o.distri_delivery_state == "delivered" or bool(o.distri_entregado_fuera)
        if o.state == "sale" and o.distri_delivery_state == "delivered" and not o.distri_entregado_fuera:
            titulo, texto = "Venta en mostrador", "Entregada en el depósito, con su factura."
        elif o.state == "sale" and facturas and not entregado:
            titulo, texto = ("Venta en mostrador", "Confirmada, armada y facturada. Falta "
                             "registrar la entrega en el depósito.")
        documentos = []
        from .fichas import ficha_url
        for factura in facturas:
            documentos.append({"tipo": "Factura", "nombre": factura.name or "borrador",
                               "url": ficha_url("factura", factura.id)})
        remitos = o._distri_remitos_texto()
        if remitos:
            documentos.append({"tipo": "Remito", "nombre": remitos, "url": REMITO_PDF % o.id})
        documentos.append({"tipo": "Pedido", "nombre": o.name,
                           "url": ficha_url("pedido", o.id)})
        enviar = ""
        if o.distri_wa_state == "staff" and o._distri_wa_customer_number():
            enviar = self._nv()._pedido()._url_enviar_al_cliente(
                o, o.partner_id.name, o._distri_wa_customer_number())
        user = request.env.user
        # a transfer declared at the delivery is not a payment yet (its
        # receipt is pending): «Cobrar» offers only what is really left
        en_camino = sum(o.distri_stop_ids.filtered(
            lambda s: s.route_id.kind == "depot" and s.transfer == "yes").mapped("transfer_amount"))
        debe = round(max(debe - en_camino, 0.0), 2)
        cobrar = None
        if debe > 0.005 and can_collect(user):
            cobrar = {"debe": debe, "debe_texto": _money(debe), "factura_id": facturas[:1].id,
                      "max": MAX_TRANSFERS}
        return {
            "id": o.id, "nombre": o.name, "cliente": o.partner_id.name or "",
            "cliente_id": o.partner_id.id,
            "total": o.amount_total, "total_texto": _money(o.amount_total),
            "titulo": titulo, "texto": texto, "estado": o.state,
            "entregado": entregado,
            "entrega": None if entregado else self._entrega(order),
            "cobrar": cobrar,
            "pagos": self._pagos(o, facturas),
            "documentos": documentos,
            "enviar": enviar,
        }

    # ---------------------------------------------------------------- «Cobrar»

    def _transfers(self):
        """The transfer slots: «comprobante_N» (file), «recibo_N» (a receipt
        stored by an earlier try), «importe_transf_N»; /cobro's shape."""
        form, files = request.httprequest.form, request.httprequest.files
        out = []
        for n in range(1, MAX_TRANSFERS + 1):
            storage = files.get("comprobante_%d" % n)
            raw_file = None
            if storage and storage.filename:
                raw_file = (storage.filename[:120], storage.read(MAX_FILE_BYTES + 1))
            out.append({"amount": form.get("importe_transf_%d" % n) or "",
                        "receipt_id": form.get("recibo_%d" % n) or False,
                        "file": raw_file, "rotation": 0})
        return out

    @staticmethod
    def _recibos(stored):
        return [{"n": n, "receipt_id": t["receipt"].id,
                 "nombre": t["receipt"].media_filename or "comprobante"}
                for n, t in enumerate(stored, 1)]

    @http.route("/nueva-venta/planilla/cobrar/<int:order_id>", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def cobrar(self, order_id, **kw):
        """«Cobrar» on the summary: /cobro/registrar's two phases
        (controllers/cobro.py) for the sale's customer, to its invoice
        («factura», the default while it owes) or «A cuenta». The questions
        come back as 409 with the flag that confirms them; the receipts
        stored by a try come back so the next one does not attach them
        again."""
        user = self._check_pc()
        order = self._venta(order_id)
        if not can_collect(user):
            return self._json({"ok": False, "error": "Tu usuario no registra cobros."},
                              status=403)
        form = request.httprequest.form
        cliente = order.sudo().partner_id.commercial_partner_id.id
        facturas = self._facturas(order.sudo()).filtered(lambda m: m.amount_residual > 0.005)
        destination, invoice_id = "account", False
        if form.get("destino") != "cuenta" and facturas:
            destination, invoice_id = "lab", facturas[:1].id
        Cobro = request.env["distri.cobro"]
        try:
            stored = Cobro.collect_receipts(cliente, self._transfers())
        except (UserError, AccessError) as exc:
            return self._json({"ok": False, "error": exc.args[0] if exc.args else str(exc)},
                              status=400)
        recibos = self._recibos(stored)
        try:
            with request.env.cr.savepoint():
                cobro = Cobro.collect(
                    cliente, destination, invoice_id=invoice_id,
                    cash_amount=form.get("efectivo") or "", transfers=stored,
                    note=form.get("nota") or "", confirmed=form.get("confirmado") == "1",
                    route_ok=form.get("ruta_ok") == "1", force=form.get("forzar") == "1",
                    over_ok=form.get("deuda_ok") == "1", credit_ok=form.get(CREDIT_OK) == "1")
        except QUESTIONS as exc:
            campo = question_field(exc)
            if isinstance(exc, RouteCashWarning):
                campo = exc.field
            return self._json({"ok": False, "pregunta": exc.args[0], "campo": campo,
                               "error": exc.args[0], "recibos": recibos}, status=409)
        except (UserError, AccessError) as exc:
            return self._json({"ok": False, "error": exc.args[0] if exc.args else str(exc),
                               "recibos": recibos}, status=400)
        texto = "Cobrado %s: %s." % (_money(cobro.amount), cobro.memo or "")
        if cobro.caja_notice:
            texto += " %s" % cobro.caja_notice
        return self._json({"ok": True, "cobro_id": cobro.id, "texto": texto,
                           "venta": self._resumen(order)})
