"""AgilPedido orders that arrive by WhatsApp (models/agilpedido_wa.py) and
the paste wizard that shares their parsing (models/importar_pedido.py).

Sample messages follow AgilPedido's fixed template (plan_integracion_
distri_neuquen.md, section 6, and the texts of its shop page). Names and
phones are made up (299 555-01xx)."""
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.main import LISTA_GENERAL, LISTA_OCULTA
from odoo.addons.distri_pedido.models.agilpedido_wa import (
    ORIGEN_AGILPEDIDO_WA, PARAM_AUTO, leer_mensaje_agilpedido,
)
from odoo.addons.distri_pedido.models.importar_pedido import importe_ar
from odoo.addons.distri_pedido.models.pedido_whatsapp import PARAM_REQUIRE, find_web_codes

MENSAJE = """Hola Distri Neuquén! Soy Carla Prueba y quiero hacer el siguiente pedido hacia *Av. Siempreviva 742 entre Mitre y Belgrano, Neuquén*:

- 2 x Alfajor Triple Pruebagil x 12 ($ 2.000,00)
- 1 x Yerba Pruebagil Suave 1kg ($ 3.000,00)
- 3 x May Actron Pruebagil 400 x 10 ($ 12.000,00)
- 1 x Qwxz Zzyq Inexistente ($ 500,00)

*Total: $ 17.500,00*

Comentario: Dejar en el kiosco de la esquina

Muchas gracias!"""

# the same order forwarded by hand from another phone, copied from a chat
REENVIADO = """Reenviado
[27/9 10:15] Carla Prueba:   Hola Distri Neuquén!  Soy Carla Prueba y quiero hacer el siguiente pedido para retirar por el local en *Depósito Centro*:

-   2 x   Alfajor Triple Pruebagil x 12   ($ 2.000,00)
*Total:  $ 2.000,00*

Muchas gracias!"""


@tagged("post_install", "-at_install", "distri_pedido_agilpedido")
class TestAgilpedidoWhatsapp(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Tmpl = cls.env["product.template"]
        Pricelist = cls.env["product.pricelist"]
        cls.params = cls.env["ir.config_parameter"].sudo()
        cls.params.set_param(PARAM_AUTO, "1")
        cls.params.set_param(PARAM_REQUIRE, "1")
        base = {"type": "consu", "sale_ok": True, "taxes_id": [Command.clear()]}
        cls.alfajor = Tmpl.create(dict(base, name="Alfajor Triple Pruebagil x 12", list_price=1000))
        cls.yerba = Tmpl.create(dict(base, name="Yerba Pruebagil Suave 1kg", list_price=3000))
        cls.actron = Tmpl.create(dict(base, name="Actron Pruebagil 400 x 10", list_price=5000))
        # the hidden «May» list and the public one (controllers/main.py)
        cls.mayorista = Pricelist.search([("name", "=", LISTA_OCULTA)], limit=1) or \
            Pricelist.create({"name": LISTA_OCULTA})
        cls.minorista = Pricelist.search([("name", "=", LISTA_GENERAL)], limit=1) or \
            Pricelist.create({"name": LISTA_GENERAL})
        cls.env["product.pricelist.item"].create({
            "pricelist_id": cls.mayorista.id, "applied_on": "1_product",
            "product_tmpl_id": cls.actron.id, "compute_price": "fixed", "fixed_price": 4000})
        cls.cliente = cls.env["res.partner"].create({
            "name": "Kiosco Pruebagil", "company_type": "company",
            "distri_whatsapp": "299 555-0142"})
        cls.admin_distri = new_test_user(
            cls.env, login="admin_agil_test", name="Administración AgilPedido",
            groups="base.group_user,distri_pedido.grupo_administracion,sales_team.group_sale_manager")
        cls.Message = cls.env["distri.whatsapp.message"].sudo()
        cls.Order = cls.env["sale.order"].sudo()

    _seq = 0

    def _payload(self, message, wa_id="5492995550101", name="Carla Prueba"):
        return {"object": "whatsapp_business_account", "entry": [{
            "id": "100000000000001", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "15551833591",
                             "phone_number_id": "100000000000002"},
                "contacts": [{"profile": {"name": name}, "wa_id": wa_id}],
                "messages": [dict({"from": wa_id, "timestamp": "1790000000"}, **message)],
            }}]}]}

    def _text(self, body, wa_id="5492995550101", msg_id=None):
        TestAgilpedidoWhatsapp._seq += 1
        msg_id = msg_id or "wamid.agil.%s" % TestAgilpedidoWhatsapp._seq
        self.Message._wa_store_webhook(self._payload(
            {"id": msg_id, "type": "text", "text": {"body": body}}, wa_id=wa_id))
        return self.Message.search([("wa_message_id", "=", msg_id)])

    def setUp(self):
        super().setUp()
        # the database copy may already hold AgilPedido orders: only this test's count
        self._previos = self.Order.search([("distri_origen", "=", ORIGEN_AGILPEDIDO_WA)]).ids

    def _agil_orders(self):
        return self.Order.search([("distri_origen", "=", ORIGEN_AGILPEDIDO_WA),
                                  ("id", "not in", self._previos)])

    # ------------------------------------------------------------ lectura

    def test_reads_the_whole_message(self):
        datos = leer_mensaje_agilpedido(MENSAJE)
        self.assertTrue(datos["es_pedido"])
        self.assertEqual(datos["nombre"], "Carla Prueba")
        self.assertEqual(datos["entrega"], "domicilio")
        self.assertEqual(datos["domicilio"], "Av. Siempreviva 742 entre Mitre y Belgrano, Neuquén")
        self.assertEqual(datos["comentario"], "Dejar en el kiosco de la esquina")
        self.assertEqual(datos["total"], 17500.0)
        self.assertTrue(datos["cierra"])
        self.assertEqual(len(datos["renglones"]), 4)
        self.assertTrue(datos["renglones"][2]["es_mayorista"])
        self.assertEqual(datos["renglones"][2]["nombre_limpio"], "Actron Pruebagil 400 x 10")
        # forwarded by hand, with the marker, a copied-chat prefix and extra spaces
        datos = leer_mensaje_agilpedido(REENVIADO)
        self.assertTrue(datos["es_pedido"])
        self.assertTrue(datos["reenviado"])
        self.assertEqual(datos["nombre"], "Carla Prueba")
        self.assertEqual(datos["entrega"], "retira")
        self.assertEqual(datos["domicilio"], "Depósito Centro")
        self.assertEqual(datos["total"], 2000.0)
        self.assertEqual(importe_ar("$ 60.680,00"), 60680.0)
        self.assertEqual(importe_ar("$1500.50"), 1500.5)
        self.assertEqual(importe_ar("$ 1.500"), 1500.0)
        # formato de los mensajes reales de AgilPedido
        self.assertEqual(importe_ar("$ 1,339,590.00"), 1339590.0)
        self.assertEqual(importe_ar("($ 10,800.00)"), 10800.0)
        self.assertEqual(importe_ar("$ 890.00"), 890.0)
        self.assertEqual(importe_ar("$ 10,800"), 10800.0)
        self.assertEqual(importe_ar("$ 1.339.590"), 1339590.0)
        self.assertEqual(importe_ar("$ 1,5"), 1.5)

    def test_what_is_not_an_order(self):
        for texto in (
            "Hola! ¿Mañana reparten por Plottier?",
            "Te paso el comprobante de la transferencia de $ 45.000",
            # a product-like line alone is not the AgilPedido message
            "- 2 x Coca (la grande)",
            "Soy Carla y quiero hacer el siguiente pedido: 2 cocas y un fernet",
        ):
            self.assertFalse(leer_mensaje_agilpedido(texto)["es_pedido"], texto)

    # ------------------------------------------------------------ alta

    def test_real_format_creates_one_draft_order(self):
        msg = self._text(MENSAJE)
        order = self._agil_orders()
        self.assertEqual(len(order), 1)
        self.assertEqual(order.state, "draft")
        self.assertEqual(order.distri_agil_message_id, msg)
        self.assertEqual(msg.distri_agil_order_id, order)
        self.assertEqual(order.distri_agil_texto, MENSAJE)
        self.assertTrue(order.partner_id.distri_generic_partner)
        self.assertEqual(order.distri_nombre, "Carla Prueba")
        self.assertEqual(order.distri_telefono, "+5492995550101")
        self.assertIn("Siempreviva 742", order.distri_domicilio)
        self.assertEqual(order.distri_entrega, "domicilio")
        self.assertIn("Dejar en el kiosco", str(order.note))
        self.assertEqual(len(order.order_line), 4)
        lines = order.order_line
        self.assertEqual(lines[0].product_id, self.alfajor.product_variant_id)
        self.assertEqual(lines[0].product_uom_qty, 2)
        self.assertEqual(lines[0].distri_agil_importe, 2000.0)
        self.assertFalse(lines[0].distri_agil_corregir)
        self.assertEqual(lines[0].distri_agil_texto,
                         "- 2 x Alfajor Triple Pruebagil x 12 ($ 2.000,00)")
        # AgilPedido's total and the difference with Odoo's
        self.assertEqual(order.distri_agil_total, 17500.0)
        self.assertTrue(order.distri_agil_cierra)
        self.assertAlmostEqual(order.distri_agil_diferencia, order.amount_total - 17500.0)
        self.assertFalse(order.user_id)
        self.assertIn("Entró por WhatsApp (AgilPedido)",
                      " ".join(order.message_ids.mapped("preview")))

    def test_webhook_retry_does_not_duplicate(self):
        self._text(MENSAJE, msg_id="wamid.agil.retry")
        self._text(MENSAJE, msg_id="wamid.agil.retry")
        self.assertEqual(len(self.Message.search([("wa_message_id", "=", "wamid.agil.retry")])), 1)
        self.assertEqual(len(self._agil_orders()), 1)
        # processing the same stored message again returns the same order
        msg = self.Message.search([("wa_message_id", "=", "wamid.agil.retry")])
        self.assertEqual(self.Order._distri_agil_desde_mensaje(msg), self._agil_orders())
        self.assertEqual(len(self._agil_orders()), 1)

    def test_may_lines_use_the_wholesale_rules(self):
        self._text(MENSAJE)
        order = self._agil_orders()
        may = order.order_line.filtered(lambda l: l.product_id == self.actron.product_variant_id)
        self.assertEqual(may.product_uom_qty, 3)
        self.assertEqual(may.price_unit, 4000)
        self.assertFalse(may.distri_agil_corregir)
        # like a page order with MAYORISTA-section lines: wholesale generic and list
        self.assertEqual(order.partner_id.distri_generic_kind, "wholesale")
        self.assertEqual(order.pricelist_id, self.mayorista)
        # a common line keeps the public price
        comun = order.order_line.filtered(lambda l: l.product_id == self.alfajor.product_variant_id)
        self.assertEqual(comun.price_unit, self.minorista._get_product_price(
            self.alfajor.product_variant_id, 2))
        # a «May» product without a wholesale price is flagged, not refused
        self._text(MENSAJE.replace("May Actron Pruebagil 400 x 10", "May Yerba Pruebagil Suave 1kg"))
        other = self._agil_orders() - order
        flagged = other.order_line.filtered("distri_agil_corregir")
        self.assertIn("precio mayorista", " ".join(flagged.mapped("distri_agil_motivo")))

    def test_unmatched_lines_are_flagged_and_cleared_when_fixed(self):
        self._text(MENSAJE)
        order = self._agil_orders()
        bad = order.order_line.filtered("distri_agil_corregir")
        self.assertEqual(len(bad), 1)
        self.assertEqual(order.distri_agil_por_corregir, 1)
        self.assertEqual(bad.distri_agil_texto, "- 1 x Qwxz Zzyq Inexistente ($ 500,00)")
        self.assertEqual(bad.distri_agil_importe, 500.0)
        self.assertEqual(bad.price_unit, 0.0)
        self.assertIn("No se encontró", bad.distri_agil_motivo)
        self.assertIn(order, self.Order.search([
            ("distri_origen", "=", ORIGEN_AGILPEDIDO_WA), ("distri_agil_por_corregir", ">", 0),
            ("state", "!=", "cancel")]))
        # not packed nor approved while a line is wrong
        self.assertFalse(order._distri_armable())
        with self.assertRaises(UserError):
            order.action_confirm()
        # «Está bien» never accepts the placeholder product
        with self.assertRaises(UserError):
            bad.with_user(self.admin_distri).action_distri_agil_renglon_ok()
        bad.with_user(self.admin_distri).write({"product_id": self.yerba.product_variant_id.id})
        self.assertFalse(bad.distri_agil_corregir)
        self.assertEqual(bad.distri_agil_texto, "- 1 x Qwxz Zzyq Inexistente ($ 500,00)")
        self.assertEqual(order.distri_agil_por_corregir, 0)
        self.assertTrue(order._distri_armable())
        # a similar name gets the product but is flagged until someone checks it
        self._text(MENSAJE.replace("Yerba Pruebagil Suave 1kg", "Yerba Pruebagil Suave 1 kilo"))
        other = self._agil_orders() - order
        similar = other.order_line.filtered(lambda l: "1 kilo" in (l.distri_agil_texto or ""))
        self.assertEqual(similar.product_id, self.yerba.product_variant_id)
        self.assertTrue(similar.distri_agil_corregir)
        self.assertIn("Parecido", similar.distri_agil_motivo)
        similar.with_user(self.admin_distri).action_distri_agil_renglon_ok()
        self.assertFalse(similar.distri_agil_corregir)

    def test_known_phone_proposes_unknown_goes_to_a_asignar(self):
        self._text(MENSAJE, wa_id="5492995550142")
        order = self._agil_orders()
        # a suggestion, never an assignment
        self.assertTrue(order.partner_id.distri_generic_partner)
        self.assertEqual(order.distri_agil_cliente_sugerido_id, self.cliente)
        self.assertIn("Kiosco Pruebagil", order.distri_agil_cliente)
        wizard = self.env["distri.asignar.cliente"].with_user(self.admin_distri).with_context(
            default_order_id=order.id).create({})
        self.assertEqual(wizard.partner_id, self.cliente)
        self.assertIn(self.cliente, wizard.candidato_ids)
        # the approval still goes through «Asignar y aprobar», after correcting
        bad = order.order_line.filtered("distri_agil_corregir")
        with self.assertRaises(UserError), self.env.cr.savepoint():
            wizard.action_asignar_y_aprobar()
        self.env.invalidate_all()
        self.assertTrue(order.partner_id.distri_generic_partner)
        bad.unlink()
        wizard.action_asignar_y_aprobar()
        self.assertEqual(order.partner_id, self.cliente)
        self.assertEqual(order.state, "sale")
        # unknown phone: to the generic contact, with no suggestion
        self._text(MENSAJE, wa_id="5492995550199")
        other = self._agil_orders() - order
        self.assertTrue(other.partner_id.distri_generic_partner)
        self.assertFalse(other.distri_agil_cliente_sugerido_id)
        self.assertEqual(other.distri_agil_cliente, "A asignar")
        self.assertEqual(other.distri_telefono, "+5492995550199")

    def test_receipts_and_plain_text_do_not_create_orders(self):
        self.Message._wa_store_webhook(self._payload({
            "id": "wamid.agil.foto", "type": "image",
            "image": {"id": "123456", "mime_type": "image/jpeg", "sha256": "x",
                      "caption": "Te mando el comprobante"}}))
        self.Message._wa_store_webhook(self._payload({
            "id": "wamid.agil.pdf", "type": "document",
            "document": {"id": "123457", "mime_type": "application/pdf",
                         "filename": "comprobante.pdf", "caption": MENSAJE}}))
        self._text("Hola, ¿mañana reparten? Gracias")
        self._text("- 2 x Coca (la grande)")
        self.assertFalse(self._agil_orders())

    def test_web_order_code_keeps_its_precedence(self):
        retail = self.env["res.partner"]._distri_generic_partner_for(False)
        web = self.Order.create({
            "partner_id": retail.id, "distri_nombre": "Kiosco Web Pruebagil",
            "distri_telefono": "2995550150",
            "order_line": [Command.create({"product_id": self.alfajor.product_variant_id.id,
                                           "product_uom_qty": 2, "price_unit": 1000})]})
        web._distri_wa_start()
        self.assertEqual(web.distri_wa_state, "waiting")
        # the page's message, even with an AgilPedido-looking body below
        texto = "Pedido %s · código %s\n\n%s" % (web.name, web.distri_wa_code, MENSAJE)
        msg = self._text(texto, wa_id="5492995550150")
        self.assertEqual(web.distri_wa_state, "received")
        self.assertEqual(msg.distri_order_id, web)
        self.assertFalse(self._agil_orders())
        # an AgilPedido order from the phone of a waiting order is a new order,
        # not a proposal to confirm the waiting one
        web2 = self.Order.create({
            "partner_id": retail.id, "distri_telefono": "2995550151",
            "order_line": [Command.create({"product_id": self.alfajor.product_variant_id.id,
                                           "product_uom_qty": 1, "price_unit": 1000})]})
        web2._distri_wa_start()
        self._text(MENSAJE, wa_id="5492995550151")
        self.assertEqual(len(self._agil_orders()), 1)
        self.assertFalse(web2.distri_wa_proposal_id)

    def test_bare_token_in_an_agilpedido_order_is_not_a_web_code(self):
        self.assertEqual(find_web_codes("Pedido S00164 · código 7k3p"), [("S00164", "7K3P")])
        self.assertEqual(find_web_codes("- 1 x Maní 85GR ($ 500,00)"), [])
        retail = self.env["res.partner"]._distri_generic_partner_for(False)
        web = self.Order.create({
            "partner_id": retail.id, "distri_telefono": "2995550170",
            "order_line": [Command.create({"product_id": self.alfajor.product_variant_id.id,
                                           "product_uom_qty": 1, "price_unit": 1000})]})
        web._distri_wa_start()
        web.distri_wa_code = "85GR"
        texto = MENSAJE.replace("- 1 x Qwxz Zzyq Inexistente ($ 500,00)",
                                "- 1 x Maní 85GR ($ 500,00)")
        msg = self._text(texto)
        self.assertEqual(web.distri_wa_state, "waiting")
        self.assertFalse(msg.distri_order_id)
        self.assertEqual(len(self._agil_orders()), 1)
        # also with the switch off: the shape alone rules out the bare token
        self.params.set_param(PARAM_AUTO, "0")
        msg = self._text(texto)
        self.assertEqual(web.distri_wa_state, "waiting")
        self.assertFalse(msg.distri_order_id)
        # the page's full line still confirms, even inside an AgilPedido-shaped text
        self._text("Pedido %s · código 85GR\n\n%s" % (web.name, texto))
        self.assertEqual(web.distri_wa_state, "received")

    def test_kill_switch(self):
        self.params.set_param(PARAM_AUTO, "0")
        self.assertFalse(self.env["distri.pedido.whatsapp.settings"].agilpedido_auto())
        msg = self._text(MENSAJE)
        self.assertTrue(msg)
        self.assertFalse(self._agil_orders())
        # the web-order code still works with the switch off
        retail = self.env["res.partner"]._distri_generic_partner_for(False)
        web = self.Order.create({
            "partner_id": retail.id, "distri_telefono": "2995550160",
            "order_line": [Command.create({"product_id": self.alfajor.product_variant_id.id,
                                           "product_uom_qty": 1, "price_unit": 1000})]})
        web._distri_wa_start()
        self._text("Pedido %s · código %s" % (web.name, web.distri_wa_code))
        self.assertEqual(web.distri_wa_state, "received")
        # the setting turns it back on
        config = self.env["distri.pedido.whatsapp.config"].with_user(self.admin_distri).create({
            "agilpedido_auto": True, "numero": "5492990000999"})
        config.action_save()
        self.assertEqual(self.params.get_param(PARAM_AUTO), "1")
        self._text(MENSAJE)
        self.assertEqual(len(self._agil_orders()), 1)
        # missing parameter: on (lab default)
        self.params.search([("key", "=", PARAM_AUTO)]).unlink()
        self.assertTrue(self.env["distri.pedido.whatsapp.settings"].agilpedido_auto())

    def test_nothing_is_sent(self):
        out_before = self.Message.search_count([("direction", "=", "out")])
        mails_before = self.env["mail.mail"].sudo().search_count([])
        self._text(MENSAJE, wa_id="5492995550142")
        order = self._agil_orders()
        self.assertTrue(order)
        self.assertEqual(self.Message.search_count([("direction", "=", "out")]), out_before)
        self.assertEqual(self.env["mail.mail"].sudo().search_count([]), mails_before)
        # the chatter only has internal notes
        self.assertFalse(order.message_ids.filtered(
            lambda m: m.message_type == "comment" and not m.subtype_id.internal))

    def test_seller_forward_does_not_suggest_the_seller(self):
        seller = new_test_user(self.env, login="vendedor_agil_test", name="Vendedor Pruebagil",
                               groups="base.group_user,distri_pedido.grupo_vendedor")
        seller.sudo().distri_whatsapp_vendedor = "2995550177"
        self.cliente.sudo().phone = "2995550177"
        self._text(REENVIADO, wa_id="5492995550177")
        order = self._agil_orders()
        self.assertEqual(len(order), 1)
        self.assertFalse(order.distri_agil_cliente_sugerido_id)
        self.assertFalse(order.distri_telefono)
        self.assertEqual(order.distri_entrega, "retira")
        self.assertIn("Vendedor Pruebagil", " ".join(order.message_ids.mapped("preview")))

    # ------------------------------------------------------------ asistente de pegar

    def test_paste_wizard_still_works(self):
        wizard = self.env["distri.importar.pedido"].with_user(self.admin_distri).create(
            {"mensaje": MENSAJE})
        wizard.action_analizar()
        self.assertEqual(len(wizard.linea_ids), 4)
        self.assertIn("1 sin identificar", wizard.resumen)
        self.assertIn("1 a precio mayorista", wizard.resumen)
        wizard.partner_id = self.cliente
        with self.assertRaises(UserError):
            wizard.action_crear_presupuesto()
        wizard.linea_ids.filtered(lambda l: not l.product_tmpl_id).unlink()
        action = wizard.action_crear_presupuesto()
        order = self.env["sale.order"].browse(action["res_id"])
        self.assertEqual(order.partner_id, self.cliente)
        self.assertEqual(len(order.order_line), 3)
        may = order.order_line.filtered(lambda l: l.product_id == self.actron.product_variant_id)
        self.assertEqual(may.price_unit, 4000)
        self.assertFalse(order.distri_origen)
