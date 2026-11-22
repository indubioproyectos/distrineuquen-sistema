"""19.0.1.129.3: the order WhatsApp text with a single /p/ link
(controllers/main.py _mensaje_whatsapp, controllers/pedido_link.py,
models/agilpedido_wa.py es_mensaje_de_la_pagina). 19.0.1.129.5 replaced
AgilPedido's layout: the format tests live in test_v129_5_enviar_empresa.py."""
from odoo import Command
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.main import TomaDePedidos
from odoo.addons.distri_pedido.models.agilpedido_wa import (
    ORIGEN_AGILPEDIDO_WA, PARAM_AUTO, es_mensaje_de_la_pagina, leer_mensaje_agilpedido,
)
from odoo.addons.distri_pedido.models.pedido_whatsapp import find_codes, find_web_codes
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin
from odoo.addons.distri_pedido.tests.test_pedido_whatsapp import PedidoWhatsappMixin

BASE = "https://distri.test"
LINK_LABEL = "Tu pedido (link personal, no lo compartas):"


class MensajeAgilMixin(PedidoWhatsappMixin):

    @classmethod
    def _setup_mensaje(cls):
        cls._setup_pedido()
        cls.env["ir.config_parameter"].sudo().set_param("web.base.url", BASE)
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_AUTO, "1")
        cls.galletitas = cls.env["product.product"].create({
            "name": "Galletitas chips v129", "list_price": 860, "sale_ok": True,
            "type": "consu", "taxes_id": [Command.clear()],
        })

    def _pedido(self, lines=None, **vals):
        values = {
            "partner_id": self.retail.id,
            "distri_nombre": "Kiosco Ana",
            "distri_telefono": "2995551234",
            "distri_domicilio": "Mitre 1",
            "distri_entrega": "domicilio",
            "order_line": [Command.create({"product_id": product.id, "product_uom_qty": qty,
                                           "price_unit": price})
                           for product, qty, price in (lines or [
                               (self.product, 2, 1000), (self.galletitas, 16, 860)])],
        }
        values.update(vals)
        order = self.env["sale.order"].sudo().create(values)
        order._distri_wa_start()
        return order

    def _texto(self, order, entrega="domicilio", mayorista=False):
        return TomaDePedidos()._mensaje_whatsapp(
            order, order.distri_nombre, order.distri_telefono, order.distri_domicilio,
            entrega, mayorista=mayorista)

    def _url(self, order):
        return "%s/p/%s" % (BASE, order.distri_link_token)

    def _cola(self, order):
        """The last block: the code line, the label and the only link."""
        return ["", order._distri_wa_code_line(), LINK_LABEL, self._url(order)]

    def _agil_orders(self):
        return self.env["sale.order"].sudo().search([("distri_origen", "=", ORIGEN_AGILPEDIDO_WA)])


@tagged("post_install", "-at_install", "distri_v129_3")
class TestMensajeAgil(MensajeAgilMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_mensaje()

    def test_only_one_link(self):
        order = self._pedido()
        order._portal_ensure_token()
        texto = self._texto(order)
        self.assertEqual(texto.count("http"), 1)
        self.assertEqual(texto.count("/p/"), 1)
        self.assertNotIn("/pedido/", texto)
        self.assertNotIn(order.access_token, texto)
        self.assertEqual(texto.split("\n")[-4:], self._cola(order))
        self.assertNotIn("Hola Distri", texto)
        self.assertNotIn("Muchas gracias", texto)

    def test_webhook_still_matches_the_code(self):
        order = self._pedido()
        texto = self._texto(order)
        # its product and total lines have AgilPedido's shape: only the page's
        # full code line counts
        self.assertTrue(leer_mensaje_agilpedido(texto)["es_pedido"])
        self.assertEqual(find_web_codes(texto), [(order.name, order.distri_wa_code)])
        self.assertIn(order.distri_wa_code, find_codes(texto))
        antes = len(self._agil_orders())
        msg = self._message(texto, wa_id="5492995551234")
        self.assertEqual(order.distri_wa_state, "received")
        self.assertEqual(msg.distri_order_id, order)
        self.assertEqual(len(self._agil_orders()), antes)

    def test_agilpedido_import_skips_the_page_text(self):
        order = self._pedido()
        texto = self._texto(order)
        self.assertTrue(es_mensaje_de_la_pagina(texto))
        antes = len(self._agil_orders())
        # the customer deleted the code line: the /p/ link still marks it as ours
        sin_codigo = texto.replace(order._distri_wa_code_line(), "")
        self.assertTrue(es_mensaje_de_la_pagina(sin_codigo))
        msg = self._message(sin_codigo, wa_id="5492995550999")
        self.assertFalse(msg.distri_agil_order_id)
        # an order code that matches no order: still never an AgilPedido draft
        ajeno = texto.replace(order.name, "S99999")
        msg = self._message(ajeno, wa_id="5492995550998")
        self.assertFalse(msg.distri_agil_order_id)
        self.assertEqual(len(self._agil_orders()), antes)
        self.assertEqual(order.distri_wa_state, "waiting")
        # without both marks the same body would be read as AgilPedido's
        sin_marcas = "\n".join(texto.split("\n")[:-3])
        self.assertTrue(leer_mensaje_agilpedido(sin_marcas)["es_pedido"])
        self.assertFalse(es_mensaje_de_la_pagina(sin_marcas))


@tagged("post_install", "-at_install", "distri_v129_3")
class TestLinkUnicoHttp(PedidoAbiertoMixin, MensajeAgilMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_mensaje()
        cls.gerente = new_test_user(
            cls.env, login="gerente_v129_3", name="Gerente v129.3",
            groups="base.group_user,distri_pedido.grupo_gerencia")
        cls.sistemas = new_test_user(
            cls.env, login="sistemas_v129_3", name="Sistemas v129.3",
            groups="base.group_user,base.group_system")
        cls.cliente = cls.env["res.partner"].create({
            "name": "Almacén Cliente v129.3", "company_type": "company"})
        cls.cliente_user = new_test_user(
            cls.env, login="cliente_v129_3", name="Almacén Cliente v129.3",
            groups="base.group_portal", partner_id=cls.cliente.id)

    def _abrir(self, order):
        return self.url_open("/p/%s" % order.distri_link_token, allow_redirects=False)

    def test_staff_go_to_the_backend(self):
        order = self._pedido()
        for login in ("admin_wa_test", "gerente_v129_3", "sistemas_v129_3"):
            self.authenticate(login, login)
            res = self._abrir(order)
            self.assertIn(res.status_code, (301, 302, 303), login)
            self.assertIn("/odoo/action-distri_pedido.action_pedidos_revisar/%s" % order.id,
                          res.headers["Location"], login)

    def test_public_goes_to_the_customer_page(self):
        order = self._pedido()
        res = self._abrir(order)
        self.assertIn(res.status_code, (301, 302, 303))
        order.invalidate_recordset(["access_token"])
        self.assertTrue(order.access_token)
        self.assertTrue(res.headers["Location"].endswith("/pedido/%s" % order.access_token))
        self.assertEqual(res.headers.get("Cache-Control"), "no-store")
        page = self.url_open("/p/%s" % order.distri_link_token)
        self.assertEqual(page.status_code, 200)
        self.assertIn(order.name, page.text)

    def test_customer_goes_to_the_customer_page(self):
        order = self._pedido(partner_id=self.cliente.id)
        order._portal_ensure_token()
        self.authenticate("cliente_v129_3", "cliente_v129_3")
        res = self._abrir(order)
        self.assertIn(res.status_code, (301, 302, 303))
        self.assertTrue(res.headers["Location"].endswith("/pedido/%s" % order.access_token))
        self.assertNotIn("/odoo/", res.headers["Location"])
