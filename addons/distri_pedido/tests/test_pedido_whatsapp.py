"""Web orders confirmed by WhatsApp and the seller's tools
(models/pedido_whatsapp.py, controllers/main.py)."""
import hashlib
import hmac
import json
import re
from datetime import timedelta
from urllib.parse import unquote

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.pedido_whatsapp import (
    PARAM_COMPANY_NUMBER, PARAM_REQUIRE, find_codes, wa_digits,
)
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido


class PedidoWhatsappMixin:

    @classmethod
    def _setup_pedido(cls):
        Partner = cls.env["res.partner"]
        cls.retail = Partner._distri_generic_partner_for(False)
        cls.product = cls.env["product.product"].create({
            "name": "Producto WhatsApp test", "list_price": 1000, "sale_ok": True,
            "type": "consu", "taxes_id": [Command.clear()],
        })
        cls.admin_distri = new_test_user(
            cls.env, login="admin_wa_test", name="Administración WA",
            groups="base.group_user,distri_pedido.grupo_administracion",
        )
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_REQUIRE, "1")
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_COMPANY_NUMBER, "15551833591")

    def _web_order(self, phone="299 555 1234", name="Kiosco WA", partner=None):
        order = self.env["sale.order"].sudo().create({
            "partner_id": (partner or self.retail).id,
            "distri_nombre": name,
            "distri_telefono": phone,
            "distri_original": json.dumps([{"id": self.product.id, "cantidad": 2}]),
            "order_line": [Command.create({"product_id": self.product.id,
                                           "product_uom_qty": 2, "price_unit": 1000})],
        })
        order._distri_wa_start()
        return order

    _seq = 0

    def _message(self, body, wa_id="5492995551234", kind="text", **extra):
        PedidoWhatsappMixin._seq += 1
        values = {
            "wa_message_id": "wamid.pedido.%s" % PedidoWhatsappMixin._seq,
            "direction": "in", "wa_id": wa_id, "profile_name": "Cliente WA",
            "message_type": kind, "body": body, "timestamp": fields.Datetime.now(),
        }
        values.update(extra)
        return self.env["distri.whatsapp.message"].sudo().create(values)


@tagged("post_install", "-at_install", "distri_pedido_whatsapp")
class TestPedidoWhatsapp(PedidoWhatsappMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_pedido()
        cls.customer = cls.env["res.partner"].create({
            "name": "Almacén WA Real", "company_type": "company"})

    def test_codes_in_text(self):
        self.assertEqual(find_codes("Pedido S00164 · código 7K3P"), ["7K3P"])
        self.assertEqual(find_codes("te mando el pedido, cod: 7k3p gracias"), ["7K3P"])
        # plain words and years are never codes
        self.assertEqual(find_codes("PARA HOLA 2024 casa"), [])
        self.assertEqual(wa_digits("299 555-1234"), "5492995551234")

    def test_waiting_order_is_out_of_the_operation(self):
        order = self._web_order()
        self.assertEqual(order.distri_wa_state, "waiting")
        self.assertRegex(order.distri_wa_code, r"^[2-9A-Z]{4}$")
        self.assertFalse(order._distri_para_armar())
        with self.assertRaises(UserError):
            order.action_confirm()
        with self.assertRaises(UserError):
            order._create_invoices()
        # routes: a confirmed and packed order still waiting is not offered
        real = self._web_order(partner=self.customer)
        real.distri_wa_state = "received"
        self.customer.distri_whatsapp = "2995551234"
        real.action_confirm()
        real.sudo().write({"distri_armado": "armado", "distri_wa_state": "waiting"})
        route = self.env["distri.route"].new({"company_id": self.env.company.id})
        domain = route._free_orders_domain() + [("id", "=", real.id)]
        self.assertFalse(self.env["sale.order"].search(domain))
        real.sudo().distri_wa_state = "received"
        self.assertTrue(self.env["sale.order"].search(domain))

    def test_switch_off_enters_directly(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_REQUIRE, "0")
        order = self._web_order()
        self.assertEqual(order.distri_wa_state, "direct")
        self.assertTrue(order._distri_para_armar())
        # a message with the code still verifies the phone
        self._message("Pedido %s · código %s" % (order.name, order.distri_wa_code))
        self.assertEqual(order.distri_wa_state, "received")
        self.assertTrue(order.distri_telefono_verificado)

    def test_webhook_code_confirms_and_verifies_phone(self):
        order = self._web_order(phone="2995559999")
        msg = self._message("Pedido %s · código %s\n\n*PEDIDO*" % (
            order.name, order.distri_wa_code), wa_id="5492995551234")
        self.assertEqual(order.distri_wa_state, "received")
        self.assertEqual(order.distri_wa_phone, "+5492995551234")
        self.assertTrue(order.distri_telefono_verificado)
        # the typed phone was another one: the verified one stays
        self.assertEqual(order.distri_telefono, "+5492995551234")
        self.assertEqual(msg.distri_order_id, order)
        self.assertTrue(order._distri_para_armar())
        # a real customer gets a verified WhatsApp and a learned phone
        real = self._web_order(partner=self.customer)
        self._message("código %s" % real.distri_wa_code.lower(), wa_id="5492994440000")
        self.assertEqual(self.customer.distri_whatsapp, "+5492994440000")
        self.assertTrue(self.customer.distri_whatsapp_confirmed)
        self.assertEqual(self.env["res.partner"]._distri_partners_by_phone("2994440000"),
                         self.customer)

    def test_edited_text_with_code_still_matches(self):
        order = self._web_order()
        self._message("hola!! les mando mi pedido, el cod es %s. gracias" %
                      order.distri_wa_code.lower())
        self.assertEqual(order.distri_wa_state, "received")

    def test_phone_only_proposes_never_confirms(self):
        order = self._web_order(phone="299 555 1234")
        msg = self._message("Hola, les hice un pedido por la página")
        self.assertEqual(order.distri_wa_state, "waiting")
        self.assertEqual(order.distri_wa_proposal_id, msg)
        order.with_user(self.admin_distri).action_distri_wa_accept_proposal()
        self.assertEqual(order.distri_wa_state, "received")
        self.assertEqual(order.distri_wa_confirmed_by_id, self.admin_distri)
        # two waiting orders from the same phone: nothing is proposed
        first, second = self._web_order(), self._web_order()
        self._message("hola de nuevo")
        self.assertFalse(first.distri_wa_proposal_id | second.distri_wa_proposal_id)

    def test_manual_confirm_and_cancel_need_a_reason(self):
        order = self._web_order()
        Wizard = self.env["distri.pedido.whatsapp.wizard"].with_user(self.admin_distri)
        action = order.with_user(self.admin_distri).action_distri_wa_confirm_manual()
        wizard = Wizard.with_context(action["context"]).create({"reason": "Llamó por teléfono"})
        wizard.action_apply()
        self.assertEqual(order.distri_wa_state, "manual")
        self.assertEqual(order.distri_wa_confirmed_by_id, self.admin_distri)
        self.assertEqual(order.distri_wa_reason, "Llamó por teléfono")
        self.assertTrue(order.distri_wa_confirmed_at)

        other = self._web_order()
        action = other.with_user(self.admin_distri).action_distri_wa_cancel()
        with self.assertRaises(Exception):
            Wizard.with_context(action["context"]).create({"reason": ""}).action_apply()
        Wizard.with_context(action["context"]).create({"reason": "Pedido repetido"}).action_apply()
        self.assertEqual(other.state, "cancel")
        self.assertEqual(other.distri_wa_cancel_reason, "Pedido repetido")
        self.assertEqual(other.distri_wa_cancelled_by_id, self.admin_distri)
        # a seller cannot confirm by hand
        seller = new_test_user(self.env, login="vend_wa_manual",
                               groups="base.group_user,distri_pedido.grupo_vendedor")
        with self.assertRaises(UserError):
            self._web_order().with_user(seller).action_distri_wa_confirm_manual()

    def test_open_chat_logs_who_reminded(self):
        order = self._web_order(phone="299 555 1234")
        action = order.with_user(self.admin_distri).action_distri_wa_open_chat()
        self.assertTrue(action["url"].startswith("https://wa.me/5492995551234?text="))
        self.assertIn(order.distri_wa_code, unquote(action["url"]))
        self.assertEqual(order.distri_wa_reminded_by_id, self.admin_distri)

    def test_nothing_cancels_a_waiting_order_by_itself(self):
        order = self._web_order()
        self.env.cr.execute("UPDATE sale_order SET create_date = %s WHERE id = %s",
                            [fields.Datetime.now() - timedelta(days=30), order.id])
        order.invalidate_recordset()
        self.assertEqual(order.distri_wa_age_level, "red")
        # no scheduled action of the module touches the order state
        for cron in self.env["ir.cron"].sudo().with_context(active_test=False).search([]):
            code = cron.code or ""
            if "distri" in (cron.ir_actions_server_id.model_id.model or "") or "distri" in code:
                self.assertNotIn("cancel", code.replace("promise", ""), cron.name)
        self.assertEqual(order.state, "draft")
        self.assertEqual(order.distri_wa_state, "waiting")

    def test_staff_and_counter_need_the_customer_whatsapp(self):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": self.product.id,
                                           "product_uom_qty": 1, "price_unit": 1000})],
        })
        order._distri_wa_start(staff=True)
        self.assertEqual(order.distri_wa_state, "staff")
        with self.assertRaisesRegex(UserError, "WhatsApp"):
            order.action_confirm()
        self.customer.distri_whatsapp = "299 400 1234"
        order.action_confirm()
        self.assertEqual(order.state, "sale")
        # «Venta en mostrador» of a waiting order: exception, but WhatsApp needed
        other = self.env["res.partner"].create({"name": "Mostrador sin WA"})
        waiting = self._web_order(partner=other)
        with self.assertRaisesRegex(UserError, "WhatsApp"):
            waiting.with_user(self.admin_distri).action_distri_counter_sale()

    def test_staff_order_without_whatsapp_confirms_when_switch_off(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.pedido_requiere_whatsapp", "0")
        customer = self.env["res.partner"].create({"name": "Cliente sin WA"})
        order = self.env["sale.order"].create({
            "partner_id": customer.id,
            "order_line": [Command.create({"product_id": self.product.id,
                                           "product_uom_qty": 1, "price_unit": 1000})],
        })
        order._distri_wa_start(staff=True)
        order.action_confirm()
        self.assertEqual(order.state, "sale")


@tagged("post_install", "-at_install", "distri_pedido_whatsapp")
class TestPedidoWhatsappHttp(PedidoAbiertoMixin, PedidoWhatsappMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_pedido()
        cls.plantilla = cls.product.product_tmpl_id
        cls.plantilla.is_published = True
        # one unit reaches the minimum purchase of /pedido (19.0.1.91.0)
        cls.plantilla.list_price = 70000.0
        cls.cartera = cls.env["res.partner.category"].create({"name": "Cartera WA test"})
        cls.seller = new_test_user(
            cls.env, login="vendedor_wa", name="Vendedor WA",
            groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.seller.sudo().etiqueta_cartera_id = cls.cartera
        cls.mine = cls.env["res.partner"].create({
            "name": "Kiosco Cartera WA", "company_type": "company",
            "distri_whatsapp": "2994112233", "category_id": [Command.link(cls.cartera.id)]})
        cls.not_mine = cls.env["res.partner"].create({
            "name": "Kiosco Ajeno WA", "company_type": "company"})

    def _post_order(self, **extra):
        data = {
            "renglones": json.dumps([{"id": self.plantilla.id, "cantidad": 2}]),
            "nombre": "Kiosco Web WA", "telefono": "2995551234",
            "domicilio": "Mitre 1", "entrega": "domicilio",
        }
        data.update(extra)
        return post_pedido(self, data)

    def test_public_order_waits_and_shows_the_step(self):
        res = self._post_order()
        self.assertEqual(res.status_code, 200)
        order = self.env["sale.order"].search([("distri_nombre", "=", "Kiosco Web WA")])
        self.assertEqual(order.distri_wa_state, "waiting")
        self.assertFalse(order.user_id)
        html = res.text
        self.assertIn("Falta un paso: enviá el mensaje por WhatsApp para que recibamos tu pedido.",
                      html)
        link = re.search(r'href="(https://wa\.me/15551833591\?text=[^"]+)"', html).group(1)
        self.assertIn("Pedido %s · código %s" % (order.name, order.distri_wa_code),
                      unquote(link.replace("&amp;", "&")))
        page = self.url_open("/pedido/%s" % order.access_token).text
        self.assertIn("Falta un paso", page)
        # the WhatsApp arrives: the page stops asking
        self._message("Pedido %s · código %s" % (order.name, order.distri_wa_code))
        self.assertNotIn("Falta un paso", self.url_open("/pedido/%s" % order.access_token).text)

    def test_signed_webhook_confirms_the_order(self):
        self._post_order(nombre="Kiosco Webhook WA")
        order = self.env["sale.order"].search([("distri_nombre", "=", "Kiosco Webhook WA")])
        secret = "secreto-de-prueba"
        self.env["ir.config_parameter"].sudo().set_param("distri_pedido.wa_app_secret", secret)
        payload = {"object": "whatsapp_business_account", "entry": [{
            "id": "100000000000001", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "15551833591",
                             "phone_number_id": "100000000000002"},
                "contacts": [{"profile": {"name": "Cliente Prueba"}, "wa_id": "5492995551234"}],
                "messages": [{"from": "5492995551234", "id": "wamid.webhook.wa.1",
                              "timestamp": "1790000000", "type": "text",
                              "text": {"body": "Pedido %s · código %s" % (
                                  order.name, order.distri_wa_code)}}],
            }}]}]}
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        res = self.url_open("/pedido/whatsapp/webhook", data=body, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": signature})
        self.assertEqual(res.status_code, 200)
        order.invalidate_recordset()
        self.assertEqual(order.distri_wa_state, "received")
        self.assertEqual(order.distri_wa_phone, "+5492995551234")

    def test_seller_order_uses_the_session_and_goes_to_the_customer(self):
        self.authenticate("vendedor_wa", "vendedor_wa")
        page = self.url_open("/pedido").text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        found = self.url_open("/pedido/clientes").json()
        self.assertEqual([f["nombre"] for f in found], ["Kiosco Cartera WA"])
        found = self.url_open("/pedido/clientes?q=Kiosco").json()
        self.assertEqual(found[0]["nombre"], "Kiosco Cartera WA")
        self.assertTrue(found[0]["mio"])
        self.assertIn("Kiosco Ajeno WA", [f["nombre"] for f in found])
        res = self._post_order(nombre="Kiosco Cartera WA", telefono="2994112233",
                               cliente_id=str(self.mine.id), csrf_token=csrf)
        self.assertEqual(res.status_code, 200)
        order = self.env["sale.order"].search([("partner_id", "=", self.mine.id)])
        self.assertEqual(order.user_id, self.seller)
        self.assertEqual(order.distri_wa_state, "staff")
        html = res.text
        self.assertIn("Enviar al cliente", html)
        link = unquote(re.search(r'href="(https://wa\.me/5492994112233\?text=[^"]+)"',
                                 html).group(1).replace("&amp;", "&"))
        self.assertIn("Hola Kiosco Cartera WA, te dejo tu pedido: ", link)
        # 19.0.1.92.0: the account is hidden from the customer by default
        self.assertIn("/pedido/%s. Desde ahí podés verlo, modificarlo y pagar."
                      % order.access_token, link)
        self.assertNotIn("ver tu cuenta", link)
        self.assertNotIn("wa.me/15551833591", html)
        # without a valid token the form is refused (19.0.1.29.0: for everyone,
        # with the page shown again with the cart instead of an error page)
        res = self._post_order(csrf_token="x")
        self.assertEqual(res.status_code, 403)
        self.assertIn("La página había vencido", res.text)

    def test_logged_out_order_has_no_seller(self):
        self._post_order(nombre="Kiosco Sin Vendedor", cliente_id=str(self.mine.id))
        order = self.env["sale.order"].search([("distri_nombre", "=", "Kiosco Sin Vendedor")])
        self.assertFalse(order.user_id)
        # the public page cannot pick a customer
        self.assertTrue(order.partner_id.distri_generic_partner)
        self.assertEqual(self.url_open("/pedido/clientes?q=Kiosco").url.split("?")[0].endswith(
            "/web/login"), True)

    def test_order_page_has_mandar_comprobante(self):
        order = self._web_order(partner=self.mine)
        order.sudo().write({"distri_wa_state": "received", "distri_armado": "armado"})
        order._portal_ensure_token()
        html = self.url_open("/pedido/%s" % order.access_token).text
        self.assertIn("Mandar comprobante", html)
        self.assertIn("https://wa.me/15551833591?text=Comprobante%%20del%%20pedido%%20%s"
                      % order.name, html)


@tagged("post_install", "-at_install", "distri_pedido_whatsapp")
class TestLinkDelPedido(PedidoAbiertoMixin, PedidoWhatsappMixin, HttpCase):
    """The order link always opens «Tu pedido»; the catalog in edit mode is
    only reached with «Modificar mi pedido» while the order is editable."""

    CATALOGO = 'action="/pedido/enviar"'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_pedido()
        cls.plantilla = cls.product.product_tmpl_id
        cls.plantilla.is_published = True
        # one unit reaches the minimum purchase of /pedido (19.0.1.91.0)
        cls.plantilla.list_price = 70000.0

    def _order(self):
        order = self._web_order(name="Despensa de Práctica")
        order._portal_ensure_token()
        return order

    def _modificar(self, order):
        return 'href="/pedido/%s/modificar"' % order.access_token

    def test_link_shows_the_summary_while_editable(self):
        order = self._order()
        self.assertTrue(order._distri_editable())
        res = self.url_open("/pedido/%s" % order.access_token)
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn("Tu pedido %s" % order.name, html)
        self.assertIn("Producto WhatsApp test", html)
        self.assertIn("Total", html)
        # still waiting for the customer's WhatsApp: the step is there
        self.assertIn("Falta tu WhatsApp", html)
        self.assertIn("Enviar por WhatsApp", html)
        self.assertIn("Modificar mi pedido", html)
        self.assertIn(self._modificar(order), html)
        # never the catalog: that is what looked like a new order
        self.assertNotIn(self.CATALOGO, html)
        self.assertNotIn("Estás modificando", html)
        # nothing about the preparation yet
        self.assertNotIn("Tu pedido quedó tal como lo mandaste", html)
        self.assertIn("Te avisamos el importe final cuando el pedido esté armado.", html)
        self.assertNotIn("Listo, actualizamos tu pedido", html)

        order.sudo().distri_wa_state = "received"
        html = self.url_open("/pedido/%s" % order.access_token).text
        self.assertIn("Recibimos tu pedido", html)
        self.assertNotIn("Enviar por WhatsApp", html)
        self.assertIn(self._modificar(order), html)

    def test_link_shows_the_summary_when_not_editable(self):
        order = self._order()
        order.sudo().write({"distri_wa_state": "received", "distri_armado": "armado"})
        self.assertFalse(order._distri_editable())
        html = self.url_open("/pedido/%s" % order.access_token).text
        self.assertIn("Tu pedido %s" % order.name, html)
        self.assertIn("Tu pedido está armado", html)
        self.assertIn("Producto WhatsApp test", html)
        self.assertNotIn("Modificar mi pedido", html)
        self.assertNotIn("/modificar", html)
        self.assertNotIn(self.CATALOGO, html)
        # packed, not invoiced yet: no promise about the packing any more
        self.assertIn("Tu pedido ya está armado. Cuando esté listo el", html)
        self.assertIn("vas a ver acá el importe final y cómo pagarlo", html)
        self.assertNotIn("cuando el pedido esté armado", html)
        # a stale «guardado» never claims a save on a closed order
        html = self.url_open("/pedido/%s?guardado=1" % order.access_token).text
        self.assertNotIn("Listo, actualizamos tu pedido", html)
        # the edit URL itself sends it back to «Tu pedido»
        res = self.url_open("/pedido/%s/modificar" % order.access_token)
        self.assertTrue(res.url.endswith("/pedido/%s" % order.access_token), res.url)
        self.assertNotIn(self.CATALOGO, res.text)

    def test_edit_url_opens_the_catalog_with_the_banner(self):
        order = self._order()
        res = self.url_open("/pedido/%s/modificar" % order.access_token)
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn(self.CATALOGO, html)
        self.assertIn('id="editando"', html)
        self.assertRegex(html, r"Estás modificando tu pedido <b>%s</b>" % order.name)
        # 19.0.1.95.0: data-up, back to «Tu pedido» without a new history entry
        self.assertIn('<a data-up="" href="/pedido/%s">Volver sin cambios</a>' % order.access_token, html)
        self.assertIn('name="token" value="%s"' % order.access_token, html)
        # the WhatsApp step lives in «Tu pedido», not next to the banner
        self.assertEqual(order.distri_wa_state, "waiting")
        self.assertNotIn("Falta un paso", html)
        self.assertNotIn("Enviar por WhatsApp", html)
        # a new order has neither the banner nor the button
        html = self.url_open("/pedido").text
        self.assertNotIn('id="editando"', html)
        self.assertNotIn("Volver sin cambios", html)

    def test_volver_sin_cambios_leaves_the_order_as_it_was(self):
        order = self._order()
        before = (order.order_line.mapped(lambda l: (l.product_id.id, l.product_uom_qty)),
                  order.amount_total, order.distri_original, order.write_date)
        catalog = self.url_open("/pedido/%s/modificar" % order.access_token).text
        volver = re.search(r'<a data-up="" href="([^"]+)">Volver sin cambios</a>', catalog).group(1)
        res = self.url_open(volver)
        self.assertIn("Tu pedido %s" % order.name, res.text)
        self.assertIn(self._modificar(order), res.text)
        order.invalidate_recordset()
        after = (order.order_line.mapped(lambda l: (l.product_id.id, l.product_uom_qty)),
                 order.amount_total, order.distri_original, order.write_date)
        self.assertEqual(before, after)
        self.assertFalse(order.distri_modificado)

    def _save_edit(self, order, cantidad=5):
        return post_pedido(self, {
            "renglones": json.dumps([{"id": self.plantilla.id, "cantidad": cantidad}]),
            "nombre": "Despensa de Práctica", "telefono": "299 555-0101",
            "domicilio": "Calle Falsa 123", "entrega": "domicilio",
            "token": order.access_token,
        })

    def test_saving_an_edit_still_updates_the_same_order(self):
        order = self._order()
        count = self.env["sale.order"].search_count([])
        res = self._save_edit(order)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.env["sale.order"].search_count([]), count)
        order.invalidate_recordset()
        self.assertTrue(order.distri_modificado)
        self.assertEqual(order.order_line.filtered("product_uom_qty").product_uom_qty, 5)
        # lands on «Tu pedido» with the notice, not on the confirmation page
        self.assertTrue(res.url.endswith("/pedido/%s?guardado=1" % order.access_token), res.url)
        self.assertIn("Listo, actualizamos tu pedido.", res.text)
        self.assertIn("Tu pedido %s" % order.name, res.text)
        self.assertNotIn("Volver al catálogo", res.text)
        self.assertIn(self._modificar(order), res.text)
        # still waiting for the WhatsApp: the step is right there, with the new total
        self.assertIn("Falta un paso", res.text)
        link = unquote(re.search(r'href="(https://wa\.me/15551833591\?text=[^"]+)"',
                                 res.text).group(1).replace("&amp;", "&"))
        self.assertIn("código %s" % order.distri_wa_code, link)
        self.assertIn("5 x Producto WhatsApp test", link)
        self.assertNotIn("Avisar el cambio por WhatsApp", res.text)

    def test_saving_an_edit_of_a_received_order_offers_the_notice(self):
        order = self._order()
        order.sudo().distri_wa_state = "received"
        res = self._save_edit(order, cantidad=3)
        self.assertIn("Listo, actualizamos tu pedido.", res.text)
        self.assertNotIn("Falta un paso", res.text)
        link = unquote(re.search(
            r'href="(https://wa\.me/15551833591\?text=[^"]+)"[^>]*>Avisar el cambio por WhatsApp',
            res.text).group(1).replace("&amp;", "&"))
        self.assertIn("_modificado por el cliente_", link)
        self.assertIn("3 x Producto WhatsApp test", link)

    def test_new_order_confirmation_is_unchanged(self):
        res = post_pedido(self, {
            "renglones": json.dumps([{"id": self.plantilla.id, "cantidad": 1}]),
            "nombre": "Almacén de Práctica", "telefono": "299 555-0102",
            "domicilio": "Calle Falsa 456", "entrega": "domicilio",
        })
        self.assertTrue(res.url.endswith("/pedido/enviar"), res.url)
        self.assertIn("Volver al catálogo", res.text)
        self.assertIn('href="/pedido"', res.text)
        self.assertNotIn("Listo, actualizamos tu pedido", res.text)

    def test_unknown_token_goes_to_a_new_order(self):
        for url in ("/pedido/no-existe", "/pedido/no-existe/modificar"):
            res = self.url_open(url)
            self.assertTrue(res.url.endswith("/pedido"), res.url)


@tagged("post_install", "-at_install", "distri_pedido_whatsapp")
class TestComprobantesPedido(PedidoWhatsappMixin, TransactionCase):
    """«Mandar comprobante» and the seller's forwarded photos into the board."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_pedido()
        cls.customer = cls.env["res.partner"].create({
            "name": "Almacén Comprobante WA", "company_type": "company",
            "vat": "20-99911111-7"})
        cls.seller = new_test_user(cls.env, login="vend_reenvio", name="Vendedora Reenvío",
                                   groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.seller.sudo().distri_whatsapp_vendedor = "299 000 7788"

    def _income(self, amount, source, cuit=False):
        card = self.env["distri.mp.collection"].sudo().create({
            "source_id": source, "amount": amount, "date": fields.Datetime.now(),
            "payer_cuit": cuit,
        })
        card._propose()
        return card

    def test_mandar_comprobante_reaches_the_board(self):
        order = self._web_order(partner=self.customer)
        card = self._income(1500, "900000001")
        self.assertEqual(card.column, "unidentified")
        self._message("Comprobante del pedido %s" % order.name, wa_id="5492995000001")
        photo = self._message("$ 1.500", wa_id="5492995000001", kind="image",
                              media_mime_type="image/jpeg", media_id="1", media_state="pending")
        self.assertEqual(photo.distri_order_ref, order.name)
        self.assertEqual(photo.distri_partner_id, self.customer)
        self.assertEqual(card.receipt_id, photo)
        self.assertEqual(card.partner_id, self.customer)
        self.assertEqual(card.column, "review")

    def test_seller_forwarded_photo_goes_to_review(self):
        # an odd amount: a real receipt of a round amount in the copy would pair
        card = self._income(2017, "900000002", cuit="20999111117")
        self.assertEqual(card.column, "confirm")
        photo = self._message("Cliente: Almacén Comprobante WA\n$ 2.017", wa_id="5492990007788",
                              kind="image", media_mime_type="image/jpeg", media_id="2",
                              media_state="pending")
        self.assertEqual(photo.distri_forwarded_by_id, self.seller)
        self.assertEqual(photo.distri_partner_id, self.customer)
        self.assertEqual(card.receipt_id, photo)
        self.assertEqual(card.column, "review")
        self.assertIn("Foto reenviada por Vendedora Reenvío", card.proposal_note)
        # confirming never learns the seller's phone as the customer's
        self.env.user.sudo().write({"group_ids": [
            Command.link(self.env.ref("distri_pedido.grupo_administracion").id)]})
        card.action_confirm()
        self.assertFalse(self.env["distri.payer.identity"].search(
            [("kind", "=", "phone"), ("value", "like", "2990007788")]))

    def test_seller_note_with_cuit(self):
        photo = self._message("es de 20-99911111-7", wa_id="5492990007788", kind="image",
                              media_mime_type="image/jpeg", media_id="3", media_state="pending")
        self.assertEqual(photo.distri_partner_id, self.customer)
        self.assertTrue(photo.distri_is_receipt)

    def test_seller_note_that_comes_late(self):
        # the photo is read at once; the seller names the customer 6 minutes later
        sent = fields.Datetime.now() - timedelta(minutes=20)
        photo = self._message("", wa_id="5492990007788", kind="image",
                              media_mime_type="image/jpeg", media_id="4", media_state="pending",
                              timestamp=sent)
        self.assertTrue(photo.distri_is_receipt)
        self.assertFalse(photo.distri_partner_id)
        self._message("Cliente: Almacén Comprobante WA", wa_id="5492990007788",
                      timestamp=sent + timedelta(minutes=6))
        self.assertEqual(photo.distri_partner_id, self.customer)
        self.assertEqual(photo.distri_partner_source, "Nota de Vendedora Reenvío junto a la foto")

    def test_late_note_never_replaces_a_surer_customer(self):
        # the note names a real customer: only the rules keep it off the photo
        self.env["res.partner"].create({"name": "Despensa Nota Tardía", "company_type": "company"})
        sent = fields.Datetime.now() - timedelta(minutes=20)
        photo = self._message("es de 20-99911111-7", wa_id="5492990007788", kind="image",
                              media_mime_type="image/jpeg", media_id="5", media_state="pending",
                              timestamp=sent)
        self.assertEqual(photo.distri_partner_id, self.customer)
        self._message("Despensa Nota Tardía", wa_id="5492990007788",
                      timestamp=sent + timedelta(minutes=5))
        self.assertEqual(photo.distri_partner_id, self.customer)
        # too late: a note 15 minutes after the photo is not about it
        lone = self._message("", wa_id="5492990007788", kind="image",
                             media_mime_type="image/jpeg", media_id="6", media_state="pending",
                             timestamp=sent - timedelta(minutes=30))
        self.assertFalse(lone.distri_partner_id)
        self._message("Despensa Nota Tardía", wa_id="5492990007788",
                      timestamp=sent - timedelta(minutes=15))
        self.assertFalse(lone.distri_partner_id)
