"""The order's short link (/p/<token>), «WhatsApp verificado» and
«Enviar código por WhatsApp» (models/pedido_link.py, controllers/pedido_link.py)."""
import json
import re
from datetime import timedelta
from urllib.parse import unquote

from psycopg2 import IntegrityError

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.distri_pedido.models.pedido_link import (
    LINK_TOKEN_LENGTH, new_link_token, valid_link_token,
)
from odoo.addons.distri_pedido.models.pedido_whatsapp import find_codes, find_web_codes
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido
from odoo.addons.distri_pedido.tests.test_pedido_whatsapp import PedidoWhatsappMixin
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin


class PedidoLinkMixin(PedidoWhatsappMixin, RepartoMixin):

    @classmethod
    def _setup_link(cls):
        cls._setup_pedido()
        cls._setup_reparto()
        cls.vendedor = new_test_user(
            cls.env, login="vendedor_link", name="Vendedor Link",
            groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.gerente = new_test_user(
            cls.env, login="gerente_link", name="Gerente Link",
            groups="base.group_user,distri_pedido.grupo_gerencia")

    @classmethod
    def _habitual(cls, name, whatsapp=False):
        """An old customer with purchases: only the WhatsApp can paint it yellow."""
        partner = cls.env["res.partner"].create({
            "name": name, "company_type": "company", "distri_whatsapp": whatsapp})
        cls.env.cr.execute("UPDATE res_partner SET create_date = %s WHERE id = %s",
                           (fields.Datetime.now() - timedelta(days=400), partner.id))
        partner.invalidate_recordset(["create_date"])
        hoy = fields.Date.today()
        cls.env["distri.cuenta.movimiento"].sudo().create([{
            "partner_id": partner.id, "tipo": "factura", "debe": 2000.0,
            "fecha": hoy - timedelta(days=10 + i), "comprobante": "%s-%s" % (name[:6], i),
        } for i in range(3)])
        return partner


@tagged("post_install", "-at_install", "distri_pedido_link")
class TestPedidoLink(PedidoLinkMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_link()

    # ------------------------------------------------------------ token

    def test_token_created_unique_and_url_safe(self):
        first = self._web_order()
        second = self._web_order(phone="299 555 4321")
        for order in first | second:
            self.assertTrue(valid_link_token(order.distri_link_token))
            self.assertEqual(len(order.distri_link_token), LINK_TOKEN_LENGTH)
            self.assertRegex(order.distri_link_token, r"^[A-Za-z0-9]+$")
        self.assertNotEqual(first.distri_link_token, second.distri_link_token)
        # it never changes once created
        token = first.distri_link_token
        first._distri_link_token_ensure()
        self.assertEqual(first.distri_link_token, token)
        # the database refuses a repeated token
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.cr.savepoint():
            second.sudo().write({"distri_link_token": token})
            second.flush_recordset()
        # a staff order gets it only when its message is built
        staff = self._web_order()
        staff.sudo().distri_link_token = False
        self.assertTrue(valid_link_token(staff._distri_link_token_ensure()))

    def test_link_finds_only_the_exact_token(self):
        order = self._web_order()
        Order = self.env["sale.order"]
        token = order.distri_link_token
        self.assertEqual(Order._distri_link_find(token), order)
        self.assertFalse(Order._distri_link_find(token.lower() if token != token.lower()
                                                 else token.upper()))
        self.assertFalse(Order._distri_link_find(token[:-1]))
        self.assertFalse(Order._distri_link_find(str(order.id)))
        self.assertFalse(Order._distri_link_find(""))
        self.assertFalse(Order._distri_link_find(token + "x"))

    def test_link_line_keeps_the_code_matching(self):
        order = self._web_order(phone="2995551234")
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url").rstrip("/")
        line = order._distri_link_line()
        self.assertEqual(line, "Ver pedido: %s/p/%s" % (base, order.distri_link_token))
        text = "%s\n%s\n\n*PEDIDO %s*\n2 x Producto WhatsApp test = $ 2.000" % (
            order._distri_wa_code_line(), line, order.name)
        self.assertEqual(find_codes(text), [order.distri_wa_code])
        self.assertEqual(find_web_codes(text), [(order.name, order.distri_wa_code)])
        # no token ever yields a 4-character code (letters and digits only)
        for _i in range(300):
            self.assertEqual(find_codes("Ver pedido: https://distrineuquen.com/p/%s"
                                        % new_link_token()), [])
        self._message(text, wa_id="5492995551234")
        self.assertEqual(order.distri_wa_state, "received")
        self.assertEqual(order.distri_wa_phone, "+5492995551234")

    def test_public_status_in_plain_words(self):
        order = self._web_order()
        self.assertEqual(order._distri_link_status(), "recibido")
        order.sudo().distri_armado = "en_armado"
        self.assertEqual(order._distri_link_status(), "en armado")
        order.sudo().action_cancel()
        self.assertEqual(order._distri_link_status(), "cancelado")

    # ------------------------------------------------------------ «WhatsApp verificado»

    def test_wa_verified_writes_the_normalized_number_and_repaints(self):
        partner = self._habitual("Almacén Link Verde")
        order = self._web_order(phone="0299 15 555-1234", partner=partner)
        self.assertEqual(order.distri_semaforo, "amarillo")
        self.assertFalse(order.distri_wa_ya_verificado)
        result = order.with_user(self.admin_distri).action_distri_wa_verificado()
        self.assertTrue(result)
        self.assertEqual(partner.distri_whatsapp, "+5492995551234")
        self.assertTrue(partner.distri_whatsapp_confirmed)
        self.assertEqual(partner.distri_whatsapp_user_id, self.admin_distri)
        self.assertEqual(order.distri_semaforo, "verde")
        self.assertTrue(order.distri_wa_ya_verificado)
        self.assertIn("Administración WA verificó el WhatsApp del cliente en el chat: "
                      "+5492995551234", order.message_ids[0].body)

    def test_wa_verified_refuses_ambiguous_numbers(self):
        partner = self._habitual("Almacén Link Ambiguo")
        order = self._web_order(phone="11 15 1515 1234", partner=partner)
        with self.assertRaisesRegex(UserError, "no se puede leer con seguridad"):
            order.with_user(self.admin_distri).action_distri_wa_verificado()
        self.assertFalse(partner.distri_whatsapp)
        self.assertEqual(order.distri_semaforo, "amarillo")

    def test_wa_verified_needs_administracion(self):
        partner = self._habitual("Almacén Link Grupos")
        order = self._web_order(phone="2995551234", partner=partner)
        with self.assertRaises(UserError):
            order.with_user(self.vendedor).action_distri_wa_verificado()
        self.assertFalse(partner.distri_whatsapp)
        # gerencia implies administración
        order.with_user(self.gerente).action_distri_wa_verificado()
        self.assertEqual(partner.distri_whatsapp, "+5492995551234")

    def test_wa_verified_without_typed_number_asks_for_it(self):
        partner = self._habitual("Almacén Link Sin Número")
        order = self._web_order(phone=False, partner=partner)
        action = order.with_user(self.admin_distri).action_distri_wa_verificado()
        self.assertEqual(action["res_model"], "distri.wa.confirm.wizard")
        self.assertEqual(action["context"]["default_then"], "verify")
        wizard = self.env["distri.wa.confirm.wizard"].with_user(self.admin_distri).with_context(
            action["context"]).create({"area": "0299", "number": "155551234"})
        with self.assertRaises(UserError):
            wizard.action_skip()
        state = order.state
        wizard.action_save_confirm()
        self.assertEqual(partner.distri_whatsapp, "+5492995551234")
        self.assertTrue(partner.distri_whatsapp_confirmed)
        self.assertEqual(order.state, state)  # stored, never confirmed
        self.assertEqual(order.distri_semaforo, "verde")

    def test_wa_verified_on_a_generic_order_marks_the_order(self):
        order = self._web_order(phone="299 555 1234")
        order.with_user(self.admin_distri).action_distri_wa_verificado()
        self.assertTrue(order.distri_telefono_verificado)
        self.assertEqual(order.distri_telefono, "+5492995551234")
        self.assertTrue(order.distri_wa_ya_verificado)

    # ------------------------------------------------------------ receipt code by WhatsApp

    def _confirmed(self, partner):
        return self._order(partner, self.product, qty=2, price=1000)

    def test_receipt_code_by_whatsapp(self):
        partner = self.env["res.partner"].create({
            "name": "Kiosco Código WA", "distri_whatsapp": "2994222444"})
        order = self._confirmed(partner)
        self.assertEqual(order.distri_receipt_wa_digits, "5492994222444")
        old_hash = order.sudo().distri_receipt_hash
        action = order.with_user(self.admin_distri).action_distri_receipt_code_whatsapp()
        self.assertEqual(action["type"], "ir.actions.act_url")
        self.assertEqual(action["target"], "new")
        self.assertTrue(action["url"].startswith("https://wa.me/5492994222444?text="))
        text = unquote(action["url"].split("?text=", 1)[1])
        code = re.search(r"del pedido %s es (\d{4})\." % re.escape(order.name), text).group(1)
        self.assertEqual(text, (
            "Hola, te escribimos de Distri Neuquén. Tu código de recibido del pedido %s es %s. "
            "Dáselo al repartidor cuando recibas el pedido." % (order.name, code)))
        self.assertNotEqual(order.sudo().distri_receipt_hash, old_hash)
        self.assertTrue(order._distri_check_receipt_code(code))
        self.assertIn("preparado para enviar por WhatsApp por Administración WA",
                      order.message_ids[0].body)
        self.assertNotIn("actualice la ruta", order.message_ids[0].body)
        # 19.0.1.99.0: the code the customer already sees is sent again as is
        # (no new code, so the driver's phone needs no refresh)
        self._route(order)
        again = order.with_user(self.admin_distri).action_distri_receipt_code_whatsapp()
        self.assertIn(code, unquote(again["url"]))
        self.assertTrue(order._distri_check_receipt_code(code))
        self.assertNotIn("que actualice la ruta", order.message_ids[0].body)
        # an order with only the hash of an old code gets a new one, with the hint
        order.sudo().distri_receipt_code = False
        order.with_user(self.admin_distri).action_distri_receipt_code_whatsapp()
        self.assertIn("que actualice la ruta", order.message_ids[0].body)

    def test_receipt_code_by_whatsapp_permissions_and_number(self):
        partner = self.env["res.partner"].create({"name": "Kiosco Sin WA Código"})
        order = self._confirmed(partner)
        self.assertFalse(order.distri_receipt_wa_digits)
        with self.assertRaises(UserError):
            order.with_user(self.admin_distri).action_distri_receipt_code_whatsapp()
        partner.distri_whatsapp = "+5492994222555"
        with self.assertRaises(UserError):
            order.with_user(self.vendedor).action_distri_receipt_code_whatsapp()
        self.assertTrue(order.with_user(self.admin_distri).action_distri_receipt_code_whatsapp())
        # a generic order: the typed phone
        generic = self._web_order(phone="0299 15 444-1122")
        self.assertEqual(generic.distri_receipt_wa_digits, "5492994441122")
        # the old action stays
        notice = order.with_user(self.admin_distri).action_distri_new_receipt_code()
        self.assertEqual(notice["tag"], "display_notification")


@tagged("post_install", "-at_install", "distri_pedido_link")
class TestPedidoLinkHttp(PedidoAbiertoMixin, PedidoLinkMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_link()
        cls.plantilla = cls.product.product_tmpl_id
        cls.plantilla.is_published = True
        # one unit reaches the minimum purchase of /pedido (19.0.1.91.0)
        cls.plantilla.list_price = 70000.0

    def test_customer_message_carries_the_link(self):
        res = post_pedido(self, {
            "renglones": json.dumps([{"id": self.plantilla.id, "cantidad": 2}]),
            "nombre": "Kiosco Link Web", "telefono": "2995551234",
            "domicilio": "Mitre 1", "entrega": "domicilio",
        })
        self.assertEqual(res.status_code, 200)
        order = self.env["sale.order"].search([("distri_nombre", "=", "Kiosco Link Web")])
        link = re.search(r'href="(https://wa\.me/15551833591\?text=[^"]+)"', res.text).group(1)
        text = unquote(link.replace("&amp;", "&").split("?text=", 1)[1])
        lines = text.splitlines()
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url").rstrip("/")
        self.assertEqual(lines[0], "Pedido %s · código %s" % (order.name, order.distri_wa_code))
        self.assertEqual(lines[1], "Ver pedido: %s/p/%s" % (base, order.distri_link_token))
        self.assertIn(order.distri_wa_code, find_codes(text))
        # the whole text, as the customer sends it, still confirms the order
        self._message(text, wa_id="5492995551234")
        self.assertEqual(order.distri_wa_state, "received")

    def test_public_page_shows_only_the_status(self):
        order = self._web_order(phone="2995557788", name="Kiosco Secreto Link")
        order._portal_ensure_token()
        res = self.url_open("/p/%s" % order.distri_link_token, allow_redirects=False)
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn("Recibimos tu pedido %s" % order.name, html)
        self.assertIn("recibido", html)
        for private in ("$", "2.000", "2000", "Kiosco Secreto Link", "5557788",
                        order.partner_id.name, order.access_token, order.distri_wa_code):
            if private:
                self.assertNotIn(private, html)
        self.assertIn("Soy del equipo: ingresar", html)
        self.assertIn("/web/login?redirect=%%2Fp%%2F%s" % order.distri_link_token, html)
        self.assertEqual(res.headers.get("Cache-Control"), "no-store")

    def test_team_goes_to_the_backend_and_others_see_the_page(self):
        order = self._web_order()
        self.authenticate("admin_wa_test", "admin_wa_test")
        res = self.url_open("/p/%s" % order.distri_link_token, allow_redirects=False)
        self.assertIn(res.status_code, (301, 302, 303))
        self.assertIn("/odoo/action-distri_pedido.action_pedidos_revisar/%s" % order.id,
                      res.headers["Location"])
        # a seller is internal but not of the team: the public page, no login link
        self.authenticate("vendedor_link", "vendedor_link")
        res = self.url_open("/p/%s" % order.distri_link_token, allow_redirects=False)
        self.assertEqual(res.status_code, 200)
        self.assertIn("Recibimos tu pedido %s" % order.name, res.text)
        self.assertNotIn("Soy del equipo", res.text)

    def test_invalid_token_is_not_found(self):
        order = self._web_order()
        with mute_logger("odoo.http"):
            for token in ("A" * LINK_TOKEN_LENGTH, str(order.id), order._portal_ensure_token(),
                          order.distri_link_token[:-1] + "_"):
                res = self.url_open("/p/%s" % token, allow_redirects=False)
                self.assertEqual(res.status_code, 404, token)
