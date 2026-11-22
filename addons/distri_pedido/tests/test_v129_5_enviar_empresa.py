"""19.0.1.129.5: the cleaner order WhatsApp text and the mandatory «Enviar a
la empresa» step (models/enviar_empresa.py, controllers/main.py,
views/enviar_empresa.xml, views/templates.xml)."""
import json
import re
from urllib.parse import unquote

from lxml import etree

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools.safe_eval import safe_eval

from odoo.addons.distri_pedido.models.agilpedido_wa import (
    ORIGEN_AGILPEDIDO_WA, es_mensaje_de_la_pagina,
)
from odoo.addons.distri_pedido.models.enviar_empresa import UNPRICED_NOTE, money
from odoo.addons.distri_pedido.models.pedido_whatsapp import find_codes, find_web_codes
from odoo.addons.distri_pedido.models.sale_order import STOCK_NOTICE
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido
from odoo.addons.distri_pedido.tests.test_v129_3_mensaje_agil import (
    BASE, LINK_LABEL, MensajeAgilMixin,
)

COMPANY_WA = "https://wa.me/15551833591?text="


def wa_text(html, prefix=COMPANY_WA):
    """The text of the first wa.me link to `prefix` in a page."""
    link = re.search(r'href="(%s[^"]+)"' % re.escape(prefix), html).group(1)
    return unquote(link.replace("&amp;", "&").split("?text=", 1)[1])


@tagged("post_install", "-at_install", "distri_v129_5")
class TestMensajeEmpresa(MensajeAgilMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_mensaje()

    def test_money_format(self):
        self.assertEqual(money(4240), "$ 4.240")
        self.assertEqual(money(50400), "$ 50.400")
        self.assertEqual(money(4980000), "$ 4.980.000")
        self.assertEqual(money(999), "$ 999")
        self.assertEqual(money(0), "$ 0")
        self.assertEqual(money(4240.4), "$ 4.240")

    def test_simple_order_exact_text(self):
        order = self._pedido()
        self.assertEqual(self._texto(order).split("\n"), [
            "*Pedido:* %s" % order.name,
            "*Cliente:* Kiosco Ana",
            "*Dirección:* Mitre 1",
            "*Teléfono:* 2995551234",
            "",
            "- 2 x Producto WhatsApp test ($ 2.000)",
            "- 16 x Galletitas chips v129 ($ 13.760)",
            "",
            "*Total: $ 15.760*",
            "",
            STOCK_NOTICE,
        ] + self._cola(order))
        self.assertNotIn("Soy ", self._texto(order))
        self.assertNotIn(",00", self._texto(order))

    def test_no_phone_no_phone_line(self):
        order = self._pedido(distri_telefono=False)
        lines = self._texto(order).split("\n")
        self.assertEqual(lines[:4], ["*Pedido:* %s" % order.name, "*Cliente:* Kiosco Ana",
                                     "*Dirección:* Mitre 1", ""])
        self.assertFalse([line for line in lines if "Teléfono" in line])

    def test_exactly_one_link_on_its_own_line(self):
        order = self._pedido()
        order._portal_ensure_token()
        lines = self._texto(order).split("\n")
        self.assertEqual(sum(line.count("http") for line in lines), 1)
        self.assertEqual(lines[-1], self._url(order))
        self.assertEqual(lines[-2], LINK_LABEL)
        self.assertEqual(lines[-3], order._distri_wa_code_line())
        self.assertNotIn(order.access_token, "\n".join(lines))

    def test_retira(self):
        order = self._pedido(distri_entrega="retira", distri_domicilio=False)
        lines = self._texto(order, entrega="retira").split("\n")
        self.assertEqual(lines[:4], ["*Pedido:* %s" % order.name, "*Cliente:* Kiosco Ana",
                                     "*Retira:* En el depósito",
                                     "*Teléfono:* 2995551234"])
        self.assertFalse([line for line in lines if "Dirección" in line])

    def test_encomienda(self):
        order = self._pedido(distri_encomienda_pedida=True, distri_encomienda_nombre="Ana Pérez",
                             distri_encomienda_dni="30123456",
                             distri_encomienda_localidad="Zapala", distri_domicilio=False)
        lines = self._texto(order).split("\n")
        self.assertEqual(lines[:6], [
            "*Pedido:* %s" % order.name,
            "*Cliente:* Kiosco Ana",
            "*Encomienda:* terminal de Zapala",
            "*Retira:* Ana Pérez, DNI 30123456",
            "*Teléfono:* 2995551234",
            "",
        ])
        self.assertFalse([line for line in lines if "Dirección" in line])

    def test_wholesale(self):
        order = self._pedido()
        lines = self._texto(order, mayorista=True).split("\n")
        self.assertEqual(lines[4:8], ["", "⚠️ *LISTA DISTRIBUIDORAS*", "",
                                      "- 2 x Producto WhatsApp test ($ 2.000)"])
        self.assertNotIn("LISTA DISTRIBUIDORAS", self._texto(order))

    def test_unpriced_lines(self):
        order = self._pedido(lines=[(self.product, 2, 1000), (self.galletitas, 3, 0)])
        texto = self._texto(order)
        self.assertIn("- 3 x Galletitas chips v129 ($ a confirmar)\n\n"
                      "*Total: $ 2.000 + a confirmar*\n" + UNPRICED_NOTE + "\n\n", texto)
        nada = self._pedido(lines=[(self.galletitas, 3, 0)], distri_telefono="2995551235")
        self.assertIn("\n\n*Total: a confirmar*\n" + UNPRICED_NOTE + "\n\n", self._texto(nada))

    def test_comentario_and_modified(self):
        order = self._pedido(distri_comentario="entregar de 9 a 14")
        order.sudo().distri_modificado = True
        lines = self._texto(order).split("\n")
        self.assertEqual(lines[0], "_modificado por el cliente_")
        self.assertEqual(lines[1], "*Pedido:* %s" % order.name)
        self.assertEqual(lines[2], "*Cliente:* Kiosco Ana")
        total = lines.index("*Total: $ 15.760*")
        self.assertEqual(lines[total + 1:total + 5],
                         ["", "*Comentario:* entregar de 9 a 14", "", STOCK_NOTICE])
        sin = self._pedido(distri_telefono="2995551236")
        self.assertNotIn("Comentario", self._texto(sin))

    def test_pack_unit_detail(self):
        docena = self.env.ref("uom.product_uom_dozen")
        order = self._pedido(lines=[(self.product, 2, 1000)])
        order.order_line.product_uom_id = docena
        order.order_line.price_unit = 1000
        self.assertIn("- 2 x Producto WhatsApp test · %s ($ 2.000)" % docena.name,
                      self._texto(order).split("\n"))

    def test_webhook_matches_and_no_agilpedido_draft(self):
        order = self._pedido()
        texto = self._texto(order)
        self.assertEqual(find_web_codes(texto), [(order.name, order.distri_wa_code)])
        self.assertIn(order.distri_wa_code, find_codes(texto))
        self.assertTrue(es_mensaje_de_la_pagina(texto))
        antes = len(self._agil_orders())
        msg = self._message(texto, wa_id="5492995551234")
        self.assertEqual(order.distri_wa_state, "received")
        self.assertEqual(msg.distri_order_id, order)
        self.assertFalse(msg.distri_agil_order_id)
        # without the code line the /p/ link still keeps it away from AgilPedido
        otro = self._pedido(distri_telefono="2995551237")
        sin_codigo = self._texto(otro).replace(otro._distri_wa_code_line(), "")
        msg = self._message(sin_codigo, wa_id="5492995550997")
        self.assertFalse(msg.distri_agil_order_id)
        self.assertEqual(len(self._agil_orders()), antes)

    # ------------------------------------------------------------ backend

    def test_backend_button(self):
        order = self._pedido()
        self.assertFalse(order.distri_enviado_empresa)
        action = order.action_distri_enviar_empresa()
        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertTrue(action["url"].startswith(COMPANY_WA))
        texto = unquote(action["url"].split("?text=", 1)[1])
        self.assertEqual(texto, order._distri_company_message_default())
        self.assertTrue(texto.startswith("*Pedido:* %s\n*Cliente:* Kiosco Ana\n*Dirección:* Mitre 1\n"
                                         % order.name))
        self.assertTrue(texto.endswith("\n" + self._url(order)))
        self.assertTrue(order.distri_enviado_empresa)
        primera = order.distri_enviado_empresa_el
        self.assertTrue(primera)
        order.action_distri_enviar_empresa()
        self.assertEqual(order.distri_enviado_empresa_el, primera)
        cancelado = self._pedido(distri_telefono="2995551238")
        cancelado._action_cancel()
        with self.assertRaises(UserError):
            cancelado.action_distri_enviar_empresa()
        self.assertFalse(cancelado.distri_enviado_empresa)

    def test_backend_text_for_a_real_customer(self):
        cliente = self.env["res.partner"].create({
            "name": "Almacén Real v129.5", "street": "Rufino Ortega y Concordia",
            "distri_whatsapp": "+5492994123456"})
        order = self.env["sale.order"].sudo().create({
            "partner_id": cliente.id,
            "order_line": [Command.create({"product_id": self.product.id,
                                           "product_uom_qty": 6, "price_unit": 8400})],
        })
        order._distri_wa_start(staff=True)
        lines = order._distri_company_message_default().split("\n")
        self.assertEqual(lines[:6], [
            "*Pedido:* %s" % order.name,
            "*Cliente:* Almacén Real v129.5",
            "*Dirección:* Rufino Ortega y Concordia",
            "*Teléfono:* +54 9 299 412-3456",
            "",
            "- 6 x Producto WhatsApp test ($ 50.400)",
        ])
        # a staff order has no code: the label and the link close it
        self.assertEqual(lines[-2:], [LINK_LABEL, self._url(order)])

    def test_form_warning_and_button(self):
        arch = self.env["sale.order"].get_view(
            self.env.ref("sale.view_order_form").id, "form")["arch"]
        self.assertIn('name="action_distri_enviar_empresa"', arch)
        self.assertIn('name="distri_falta_empresa"', arch)

    def test_review_filter_and_column(self):
        Order = self.env["sale.order"]
        search = Order.get_view(
            self.env.ref("distri_pedido.view_order_search_revisar").id, "search")["arch"]
        node = etree.fromstring(search).xpath("//filter[@name='no_enviado_empresa']")[0]
        self.assertEqual(node.get("string"), "No enviado a la empresa")
        dominio = safe_eval(node.get("domain"))
        falta = self._pedido()
        enviado = self._pedido(distri_telefono="2995551239")
        enviado._distri_mark_sent_to_company()
        agil = self._pedido(distri_telefono="2995551240")
        agil.sudo().distri_origen = ORIGEN_AGILPEDIDO_WA
        cancelado = self._pedido(distri_telefono="2995551241")
        cancelado._action_cancel()
        ids = (falta | enviado | agil | cancelado).ids
        self.assertEqual(Order.sudo().search(dominio + [("id", "in", ids)]), falta)
        lista = Order.get_view(
            self.env.ref("distri_pedido.view_order_list_revisar").id, "list")["arch"]
        self.assertIn('name="distri_enviado_empresa"', lista)


@tagged("post_install", "-at_install", "distri_v129_5")
class TestEnviarEmpresaHttp(PedidoAbiertoMixin, MensajeAgilMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_mensaje()
        cls.plantilla = cls.product.product_tmpl_id
        cls.plantilla.is_published = True
        # one unit reaches the minimum purchase of /pedido (19.0.1.91.0)
        cls.plantilla.list_price = 70000.0
        cls.cartera = cls.env["res.partner.category"].create({"name": "Cartera v129.5"})
        cls.seller = new_test_user(
            cls.env, login="vendedor_v129_5", name="Vendedor v129.5",
            groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.seller.sudo().etiqueta_cartera_id = cls.cartera
        cls.mine = cls.env["res.partner"].create({
            "name": "Kiosco Cartera v129.5", "company_type": "company",
            "distri_whatsapp": "2994112233", "category_id": [Command.link(cls.cartera.id)]})

    def _post_order(self, **extra):
        data = {
            "renglones": json.dumps([{"id": self.plantilla.id, "cantidad": 2}]),
            "nombre": "Kiosco Web v129.5", "telefono": "2995551234",
            "domicilio": "Mitre 1", "entrega": "domicilio",
        }
        data.update(extra)
        return post_pedido(self, data)

    def _marcar(self, order):
        return self.url_open("/pedido/%s/enviado-empresa" % order.access_token, data={"t": "1"})

    def test_seller_flow_shows_the_company_step(self):
        self.authenticate("vendedor_v129_5", "vendedor_v129_5")
        res = self._post_order(nombre="Kiosco Cartera v129.5", telefono="2994112233",
                               cliente_id=str(self.mine.id))
        self.assertEqual(res.status_code, 200)
        order = self.env["sale.order"].search([("partner_id", "=", self.mine.id)])
        self.assertEqual(order.distri_wa_state, "staff")
        html = res.text
        self.assertIn("Enviar al cliente", html)
        self.assertIn("Enviar a la empresa", html)
        self.assertIn("Falta un paso: enviá el pedido a la empresa.", html)
        self.assertNotIn("No hace falta mandarlo a la empresa", html)
        self.assertIn('data-enviado-url="/pedido/%s/enviado-empresa"' % order.access_token, html)
        texto = wa_text(html)
        self.assertTrue(texto.startswith("*Pedido:* %s\n*Cliente:* Kiosco Cartera v129.5\n"
                                         % order.name), texto)
        self.assertIn("\n\n*Total: $ 140.000*\n\n", texto)
        self.assertTrue(texto.endswith("%s\n%s/p/%s" % (LINK_LABEL, BASE, order.distri_link_token)))
        self.assertEqual(texto.count("http"), 1)
        self.assertFalse(order.distri_enviado_empresa)
        # the tap marks it
        res = self._marcar(order)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"ok": True})
        order.invalidate_recordset()
        self.assertTrue(order.distri_enviado_empresa)
        self.assertTrue(order.distri_enviado_empresa_el)

    def test_customer_order_marks_the_flag(self):
        res = self._post_order()
        order = self.env["sale.order"].search([("distri_nombre", "=", "Kiosco Web v129.5")])
        html = res.text
        self.assertIn('data-enviado-url="/pedido/%s/enviado-empresa"' % order.access_token, html)
        texto = wa_text(html)
        self.assertTrue(texto.startswith("*Pedido:* %s\n*Cliente:* Kiosco Web v129.5\n"
                                         "*Dirección:* Mitre 1\n" % order.name))
        self.assertIn(order._distri_wa_code_line(), texto.split("\n"))
        # the order page's own «Enviar por WhatsApp» posts too
        page = self.url_open("/pedido/%s" % order.access_token).text
        self.assertEqual(order.distri_wa_state, "waiting")
        self.assertIn('data-enviado-url="/pedido/%s/enviado-empresa"' % order.access_token, page)
        self.assertFalse(order.distri_enviado_empresa)
        self.assertEqual(self._marcar(order).json(), {"ok": True})
        order.invalidate_recordset()
        self.assertTrue(order.distri_enviado_empresa)

    def test_route_refuses_unknown_and_cancelled(self):
        self.assertEqual(self.url_open("/pedido/no-existe/enviado-empresa", data={"t": "1"}).status_code,
                         404)
        order = self._pedido()
        order._portal_ensure_token()
        order._action_cancel()
        self.assertEqual(self._marcar(order).status_code, 404)
        order.invalidate_recordset()
        self.assertFalse(order.distri_enviado_empresa)
