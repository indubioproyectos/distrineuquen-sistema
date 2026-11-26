"""129.49 hotfix (owner, 26/11/2026; audit of production of the same day):

1. an invoice of the packing circuit is never reset to draft nor cancelled,
   from the form or the data API, by administración or gerencia; the
   superuser, «Rearmar» and «Anular venta» still work; «Facturación directa
   de servicios» invoices are cancelled as before;
2. a confirmed order of the circuit gets no line added, removed or changed
   through the backend (form, data API), with the message that names the
   buttons to use; quotations and the module's own flows are untouched;
3. «Agregar a este cliente» on the order screen and at the counter's depot
   delivery opens «Nueva venta» with the customer (and «Mostrador»);
4. «Rearmar» returns the old remito: the stock is deducted once;
5. «Aprobar» says why it refuses and how to go on.
The blocks never touch the ledger. Synthetic data only.
"""
import re

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.controllers.main import LISTA_GENERAL
from odoo.addons.distri_pedido.models.bloqueo_factura import (
    REABRIR_OK, desde_el_sistema)
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA
from odoo.addons.distri_pedido.models.pedido_whatsapp import PARAM_REQUIRE
from odoo.addons.distri_pedido.models.remito import numerar_remitos
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

CSRF_RE = re.compile(r'(?:name="csrf_token" value|name="csrf-token" content)="([^"]+)"')
HUELLA_RE = re.compile(r'name="huella" value="([^"]+)"')
FACTURA_MSG = "no se puede reabrir ni cancelar"
LINEAS_MSG = "ya está confirmado: no se le agregan, sacan ni cambian productos"


@tagged("post_install", "-at_install", "distri_v129_49_1")
class TestV129491Bloqueo(PedidoAbiertoMixin, RepartoMixin, AccountTestInvoicingHttpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.env["res.config.settings"].create({"group_product_pricelist": True}).execute()
        cls.env["website"].search([]).company_id = cls.env.company
        Pricelist = cls.env["product.pricelist"]
        if not Pricelist.search([("name", "=", LISTA_GENERAL)], limit=1):
            Pricelist.create({"name": LISTA_GENERAL, "company_id": False,
                              "currency_id": cls.env.company.currency_id.id})
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        # Denis and Gonzalo are also in «Facturación directa de servicios»
        cls.admin = user("admin_v1491", "distri_pedido.grupo_administracion,"
                                        "distri_pedido.grupo_facturacion_directa")
        cls.gerente = user("gerente_v1491", "distri_pedido.grupo_gerencia,"
                                            "distri_pedido.grupo_facturacion_directa")
        cls.vendedor = user("vendedor_v1491", "distri_pedido.grupo_vendedor")
        cls.armador = user("armador_v1491", "distri_pedido.grupo_armador")
        Tmpl = cls.env["product.template"]
        cls.p1 = Tmpl.create({"name": "Gaseosa V1491", "type": "consu", "is_storable": True,
                              "list_price": 1000.0, "is_published": True, "sale_ok": True,
                              "invoice_policy": "order", "taxes_id": [Command.clear()]})
        cls.p2 = Tmpl.create({"name": "Galletitas V1491", "type": "consu", "is_storable": True,
                              "list_price": 500.0, "is_published": True, "sale_ok": True,
                              "invoice_policy": "order", "taxes_id": [Command.clear()]})
        cls.flete = Tmpl.create({"name": "Flete V1491", "type": "service", "list_price": 300.0,
                                 "sale_ok": True, "taxes_id": [Command.clear()]})
        cls.stock = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.env.company.id)], limit=1).lot_stock_id
        for product in (cls.p1, cls.p2):
            cls.env["stock.quant"]._update_available_quantity(
                product.product_variant_id, cls.stock, 100.0)
        cls.cliente = cls.env["res.partner"].create({
            "name": "Almacen Bloqueo V1491", "company_type": "company",
            "distri_whatsapp": "2995001491", "street": "Mitre 1491", "city": "Neuquén",
            "user_id": cls.vendedor.id})
        cls.sin_wa = cls.env["res.partner"].create({
            "name": "Kiosco Sin WhatsApp V1491", "company_type": "company",
            "street": "Roca 1491", "city": "Neuquén", "user_id": cls.vendedor.id})
        numerar_remitos(cls.env)

    # ------------------------------------------------------------ helpers

    def setUp(self):
        super().setUp()
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param(PARAM_NUEVA_VENTA, "1")
        Param.set_param(PARAM_UI_V2, "1")
        Param.set_param(PARAM_REQUIRE, "0")

    def _ledger(self):
        self.env.flush_all()
        self.cliente.invalidate_recordset(["credit"])
        return (self.env["distri.promised.transfer"]._distri_ledger_fingerprint(),
                round(self.cliente.commercial_partner_id.credit, 2))

    def _quotation(self, partner=None, user=None, q1=10, q2=5):
        Order = self.env["sale.order"].with_user(user or self.admin)
        return Order.create({
            "partner_id": (partner or self.cliente).id,
            "order_line": [
                Command.create({"product_id": self.p1.product_variant_id.id,
                                "product_uom_qty": q1, "price_unit": 1000.0,
                                "tax_ids": [Command.clear()]}),
                Command.create({"product_id": self.p2.product_variant_id.id,
                                "product_uom_qty": q2, "price_unit": 500.0,
                                "tax_ids": [Command.clear()]}),
            ]})

    def _confirmed(self):
        order = self._quotation().sudo()
        order.action_confirm()
        return order

    def _pack(self, order):
        """The packer's steps: the remito is validated and the invoice posted."""
        pedido = order.with_user(self.armador).sudo()
        self.assertEqual(pedido.distri_tomar(), "")
        self.assertEqual(pedido.distri_terminar(1), "")
        self.assertTrue(order.picking_ids.filtered(lambda p: p.state == "done"))
        return order

    def _packed(self):
        order = self._pack(self._confirmed())
        invoice = self._invoice(order)
        self.assertEqual(invoice.state, "posted")
        return order

    def _invoice(self, order):
        return order.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state != "cancel")

    def _on_hand(self, tmpl):
        """On hand in the stock location (reservations do not count)."""
        self.env.invalidate_all()
        return tmpl.product_variant_id.with_context(location=self.stock.id).qty_available

    def _services_invoice(self):
        """«Facturación directa de servicios»: no sale order, a service line."""
        move = self.env["account.move"].with_user(self.admin).create({
            "move_type": "out_invoice", "partner_id": self.cliente.id,
            "invoice_line_ids": [Command.create({
                "product_id": self.flete.product_variant_id.id, "quantity": 1,
                "price_unit": 300.0, "tax_ids": [Command.clear()]})]})
        move.action_post()
        return move.with_user(self.env.user)

    def _rpc(self, model, method, args, kwargs=None, route="/web/dataset/call_kw"):
        """JSON-RPC as the logged-in user: (result, error message)."""
        res = self.opener.post(self.base_url() + route, json={
            "id": 0, "jsonrpc": "2.0", "method": "call",
            "params": {"model": model, "method": method, "args": args,
                       "kwargs": kwargs or {}}}, timeout=30)
        res.raise_for_status()
        data = res.json()
        self.env.invalidate_all()
        if "error" in data:
            return None, data["error"]["data"].get("message") or data["error"]["data"]["name"]
        return data.get("result"), ""

    def _csrf(self, url):
        page = self.url_open(url)
        self.assertEqual(page.status_code, 200, page.text[:300])
        return CSRF_RE.search(page.text).group(1), page.text

    # ------------------------------------------------------------ 1. invoices

    def test_circuit_invoice_cannot_be_reset_or_cancelled_by_staff(self):
        order = self._packed()
        invoice = self._invoice(order)
        self.assertTrue(invoice._distri_factura_circuito())
        before = self._ledger()
        for user in (self.admin, self.gerente):
            move = invoice.with_user(user)
            self.assertFalse(move.show_reset_to_draft_button, "hidden in the form")
            for call in (move.button_draft, move.button_cancel,
                         lambda m=move: m.write({"state": "draft"}),
                         lambda m=move: m.write({"state": "cancel"})):
                with self.assertRaisesRegex(UserError, FACTURA_MSG):
                    call()
            self.assertEqual(invoice.state, "posted")
        with self.assertRaisesRegex(UserError, "Anular venta"):
            invoice.with_user(self.admin).button_draft()
        self.assertEqual(self._ledger(), before, "nothing touched the ledger")
        # the superuser (odoo shell scripts) still can
        self.assertTrue(invoice.sudo().show_reset_to_draft_button)
        invoice.sudo().button_draft()
        self.assertEqual(invoice.state, "draft")

    def test_cancelled_circuit_invoice_is_not_reopened(self):
        order = self._packed()
        invoice = self._invoice(order)
        invoice.sudo().button_cancel()
        with self.assertRaisesRegex(UserError, FACTURA_MSG):
            invoice.with_user(self.admin).button_draft()
        self.assertEqual(invoice.state, "cancel")

    def test_rpc_bypass_is_refused(self):
        order = self._packed()
        invoice = self._invoice(order)
        line = order.order_line[:1]
        before = self._ledger()
        for user in (self.admin, self.gerente):
            self.authenticate(user.login, user.login)
            for method, args in (("button_draft", [invoice.ids]),
                                 ("button_cancel", [invoice.ids]),
                                 ("write", [invoice.ids, {"state": "draft"}]),
                                 ("write", [invoice.ids, {"state": "cancel"}])):
                _result, error = self._rpc("account.move", method, args)
                self.assertIn(FACTURA_MSG, error, (user.login, method, args))
            _result, error = self._rpc("account.move", "button_draft", [invoice.ids],
                                       route="/web/dataset/call_button")
            self.assertIn(FACTURA_MSG, error)
            self.assertEqual(invoice.state, "posted")
            # the order's lines: write, add, remove, through the order too
            calls = (
                ("sale.order.line", "write", [line.ids, {"product_uom_qty": 99}]),
                ("sale.order.line", "write", [line.ids, {"price_unit": 1.0}]),
                ("sale.order.line", "unlink", [line.ids]),
                ("sale.order.line", "create", [[{
                    "order_id": order.id, "product_id": self.p2.product_variant_id.id,
                    "product_uom_qty": 25}]]),
                ("sale.order", "write", [order.ids, {"order_line": [Command.create({
                    "product_id": self.p2.product_variant_id.id, "product_uom_qty": 25})]}]),
                ("sale.order", "write", [order.ids, {"order_line": [
                    Command.update(line.id, {"product_uom_qty": 1})]}]),
                ("sale.order", "web_save", [order.ids, {"order_line": [
                    Command.delete(line.id)]}], ),
            )
            for model, method, args in calls:
                kwargs = {"specification": {}} if method == "web_save" else {}
                _result, error = self._rpc(model, method, args, kwargs)
                self.assertIn(LINEAS_MSG, error, (user.login, model, method))
                self.assertIn("Rearmar", error)
                self.assertIn("Agregar a este cliente", error)
                self.assertNotIn("Set the quantity to 0", error)
            self.assertEqual(sorted(order.order_line.mapped("product_uom_qty")), [5.0, 10.0])
            self.assertEqual(len(order.order_line), 2)
        self.assertEqual(self._ledger(), before)

    def test_quotation_and_service_only_orders_stay_editable(self):
        self.authenticate(self.admin.login, self.admin.login)
        quote = self._quotation()
        line = quote.order_line[:1]
        _result, error = self._rpc("sale.order.line", "write", [line.ids, {"product_uom_qty": 7}])
        self.assertEqual(error, "")
        self.assertEqual(line.product_uom_qty, 7)
        # a confirmed order with services only is not of the circuit
        servicios = self.env["sale.order"].create({
            "partner_id": self.cliente.id,
            "order_line": [Command.create({"product_id": self.flete.product_variant_id.id,
                                           "product_uom_qty": 1, "price_unit": 300.0,
                                           "tax_ids": [Command.clear()]})]})
        servicios.action_confirm()
        _result, error = self._rpc("sale.order.line", "write",
                                   [servicios.order_line.ids, {"price_unit": 350.0}])
        self.assertEqual(error, "")
        self.assertEqual(servicios.order_line.price_unit, 350.0)

    def test_services_invoice_still_cancellable(self):
        move = self._services_invoice()
        self.assertFalse(move._distri_factura_circuito())
        self.assertTrue(move.with_user(self.admin).show_reset_to_draft_button)
        move.with_user(self.admin).button_draft()
        self.assertEqual(move.state, "draft")
        move.with_user(self.admin).action_post()
        self.authenticate(self.gerente.login, self.gerente.login)
        _result, error = self._rpc("account.move", "button_cancel", [move.ids])
        self.assertEqual(error, "")
        self.assertEqual(move.state, "cancel")

    def test_draft_circuit_invoice_is_cancelled_as_before(self):
        """A draft never reached a balance: «Cancelar» works (and sale's own
        cancel, which cancels the draft invoices)."""
        order = self._confirmed()
        self.env["ir.config_parameter"].sudo().set_param(PARAM_NUEVA_VENTA, "0")
        draft = order.with_context(distri_facturar_sin_armar=True)._create_invoices()
        self.assertEqual(draft.state, "draft")
        draft.with_user(self.admin).button_cancel()
        self.assertEqual(draft.state, "cancel")

    def test_module_flows_without_request_are_untouched(self):
        """Scripts and the module's own methods (no HTTP request) are not
        the backend: the packer's missing item still lowers the line."""
        self.assertFalse(desde_el_sistema())
        order = self._confirmed()
        pedido = order.with_user(self.armador).sudo()
        self.assertEqual(pedido.distri_tomar(), "")
        line = order.order_line.filtered(lambda l: l.product_id == self.p2.product_variant_id)
        pedido.distri_marcar(line, 2)
        self.assertEqual(pedido.distri_terminar(1), "")
        self.assertEqual(line.product_uom_qty, 3)
        # «Rearmar»'s context key is the one way past the invoice block
        self.assertTrue(self.env["account.move"].with_context(
            **{REABRIR_OK: True}).with_user(self.admin)._distri_reabrir_permitido())

    # ------------------------------------------------------------ the module's flows

    def test_anular_venta_still_works(self):
        order = self._packed()
        invoice = self._invoice(order)
        stock_before = self._on_hand(self.p1)
        self.authenticate(self.admin.login, self.admin.login)
        csrf, html = self._csrf("/pedidos/%s/anular" % order.id)
        res = self.url_open("/pedidos/%s/anular" % order.id, data={
            "csrf_token": csrf, "motivo": "Prueba de anulación",
            "huella": HUELLA_RE.search(html).group(1)}, allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:500])
        self.env.invalidate_all()
        self.assertEqual(order.state, "cancel")
        self.assertEqual(invoice.state, "posted", "reversed, never reset")
        self.assertEqual(invoice.payment_state, "reversed")
        self.assertEqual(self._on_hand(self.p1), stock_before + 10)

    def test_depot_return_still_works(self):
        order = self._packed()
        line = order.order_line.filtered(lambda l: l.product_id == self.p2.product_variant_id)
        stock_before = self._on_hand(self.p2)
        action = order.with_user(self.admin).action_distri_depot_delivery()
        wizard = self.env[action["res_model"]].with_user(self.admin).with_context(
            action["context"]).create({
                "returns_answer": "yes", "code_option": "without_code",
                "returns_received": True, "cash": 0, "no_payment": True})
        self.env["distri.depot.delivery.return"].with_user(self.admin).create({
            "wizard_id": wizard.id, "order_line_id": line.id, "quantity": 1,
            "reason": "Roto"})
        wizard.action_confirm()
        ret = order.distri_stop_ids.return_ids
        self.assertEqual(ret.credit_note_id.state, "posted")
        self.assertEqual(self._on_hand(self.p2), stock_before + 1)
        self.assertEqual(self._invoice(order).state, "posted")

    # ------------------------------------------------------------ «Rearmar»

    def test_rearmar_deducts_the_stock_once(self):
        """Before 129.49 hotfix the old remito stayed done and the new packing
        deducted the same goods again: 100 - 10 - 10 = 80 instead of 90."""
        self.assertEqual(self._on_hand(self.p1), 100)
        order = self._packed()
        invoice = self._invoice(order)
        self.assertEqual(self._on_hand(self.p1), 90)
        self.assertEqual(self._on_hand(self.p2), 95)
        accion = order.with_user(self.admin).with_context(
            distri_rearmar_motivo="Cambio del cliente").action_distri_rearmar()
        nuevo = self.env["sale.order"].browse(accion["res_id"])
        self.assertEqual(order.state, "cancel")
        self.assertEqual(invoice.state, "cancel")
        returned = order.picking_ids.filtered(lambda p: p.picking_type_code == "incoming")
        self.assertEqual(returned.state, "done")
        self.assertTrue(returned.name.startswith("DEV-"), returned.name)
        self.assertIn("rearmado %s" % order.name, returned.origin)
        self.assertEqual(self._on_hand(self.p1), 100, "the old remito came back")
        self.assertEqual(self._on_hand(self.p2), 100)
        self.assertIn("Volvió al stock", " ".join(str(m.body) for m in nuevo.message_ids))
        self._pack(nuevo)
        self.assertEqual(self._on_hand(self.p1), 90, "deducted once")
        self.assertEqual(self._on_hand(self.p2), 95)
        self.assertEqual(self._invoice(nuevo).state, "posted")

    def test_rearmar_from_the_backend_button(self):
        """«Rearmar» from the order form (/web/dataset/call_button) is not
        stopped by the line or invoice blocks."""
        order = self._packed()
        self.authenticate(self.admin.login, self.admin.login)
        result, error = self._rpc("sale.order", "action_distri_rearmar", [order.ids],
                                  {"context": {"distri_rearmar_motivo": "Corrección"}},
                                  route="/web/dataset/call_button")
        self.assertEqual(error, "")
        self.assertEqual(order.state, "cancel")
        nuevo = self.env["sale.order"].browse(result["res_id"])
        self.assertEqual(nuevo.state, "sale")
        self.assertEqual(self._on_hand(self.p1), 100)

    # ------------------------------------------------------------ «Agregar a este cliente»

    def test_agregar_a_este_cliente_on_the_order_screen(self):
        order = self._confirmed()
        url = "/nueva-venta?cliente=%s" % self.cliente.id
        self.authenticate(self.admin.login, self.admin.login)
        html = self.url_open("/pedidos/%s" % order.id).text
        self.assertIn('href="%s"' % url, html)
        self.assertIn("Agregar a este cliente", html)
        # a counter sale suggests «Mostrador» again
        order.distri_wa_state = "counter"
        html = self.url_open("/pedidos/%s" % order.id).text
        self.assertIn('href="%s&amp;salida=mostrador"' % url, html)
        self.assertEqual(self.url_open(url + "&salida=mostrador").status_code, 200)
        # a seller has no «Mostrador»: only the customer
        self.authenticate(self.vendedor.login, self.vendedor.login)
        html = self.url_open("/pedidos/%s" % order.id).text
        self.assertIn('href="%s"' % url, html)
        self.assertNotIn("salida=mostrador", html)
        # a quotation is still edited as a quotation: no button
        quote = self._quotation()
        self.authenticate(self.admin.login, self.admin.login)
        self.assertNotIn("Agregar a este cliente", self.url_open("/pedidos/%s" % quote.id).text)
        # «Nueva venta» off: no button
        self.env["ir.config_parameter"].sudo().set_param(PARAM_NUEVA_VENTA, "0")
        self.assertNotIn("Agregar a este cliente", self.url_open("/pedidos/%s" % order.id).text)

    def test_agregar_a_este_cliente_at_the_counter(self):
        order = self._packed()
        self.authenticate(self.admin.login, self.admin.login)
        res = self.url_open("/deposito/entrega/%s?mostrador=1" % order.id)
        self.assertEqual(res.status_code, 200, res.text[:400])
        self.assertIn('href="/nueva-venta?cliente=%s&amp;salida=mostrador"' % self.cliente.id,
                      res.text)
        res = self.url_open("/deposito/entrega/%s" % order.id)
        self.assertNotIn("Agregar a este cliente", res.text, "only at the counter")

    # ------------------------------------------------------------ «Aprobar»

    def _aprobar(self, order, **data):
        csrf, _html = self._csrf("/pedidos/%s" % order.id)
        data["csrf_token"] = csrf
        res = self.url_open("/pedidos/%s/aprobar" % order.id, data=data, allow_redirects=False)
        self.env.invalidate_all()
        return res

    def test_aprobar_says_what_is_missing(self):
        before = self._ledger()
        self.authenticate(self.admin.login, self.admin.login)
        quote = self._quotation(self.sin_wa)
        res = self._aprobar(quote)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Falta el WhatsApp del cliente", res.text)
        self.assertIn("código de área", res.text)
        self.assertIn("Aprobar sin WhatsApp", res.text)
        res = self._aprobar(quote, area="299", numero="12")
        self.assertEqual(res.status_code, 400)
        self.assertIn("no es válido", res.text)
        self.assertIn('value="12"', res.text, "what was typed stays")
        self._require_wa_on()
        res = self._aprobar(quote, sin_whatsapp="1")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Hace falta para confirmar", res.text)
        self.assertEqual(quote.state, "draft")
        held = self._quotation()
        held.sudo().distri_retenido = True
        res = self._aprobar(held)
        self.assertEqual(res.status_code, 400)
        self.assertIn("No se aprobó: Está retenido", res.text)
        self.assertEqual(self._ledger(), before)

    def _require_wa_on(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_REQUIRE, "1")
