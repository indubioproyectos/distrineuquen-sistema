"""19.0.1.129.49 (owner, 26/11): /pedido asks first for the person's name
and surname (required) and then, optional, the shop's name; both are kept
on the order (distri_nombre, distri_comercio), shown to staff, matched when
the customer is assigned, and printed as «Nombre Apellido · Comercio» in
the order texts. Plus /pedido's steps in the browser history
(static/src/js/pedido_pasos.js, tests/data/pedido_v129_49.js) and the PC
account of /cuentas in full width (tests/data/cuentas_v129_43.js).
Synthetic data only."""
import json
import os
import re
import shutil
import subprocess
from unittest import SkipTest
from urllib.parse import unquote

from odoo import Command
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.revisar import card
from odoo.addons.distri_pedido.models.sale_order import nombre_web
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido

PERSONA = "Ana Pérez Vcc"
COMERCIO = "Almacén Girasol Vcc"


@tagged("post_install", "-at_install", "distri_v129_49")
class TestNombreYComercio(PedidoAbiertoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("distri_pedido.pedido_limite_sesion_10min", "100")
        params.set_param("distri_pedido.pedido_limite_ip_10min", "100")
        cls.plantilla = cls.env["product.template"].create({
            "name": "Producto Nombre V149", "list_price": 70000.0, "is_published": True,
            "sale_ok": True, "type": "consu", "taxes_id": [Command.clear()]})
        cls.vendedor = new_test_user(cls.env, login="vendedor_v149", name="Vendedor V149",
                                     groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.admin = new_test_user(cls.env, login="admin_v149", name="Admin V149",
                                  groups="base.group_user,distri_pedido.grupo_administracion")
        Partner = cls.env["res.partner"]
        # on file by the person's name (town prefix and shop after it) ...
        cls.por_persona = Partner.create({"name": "Y - PEREZ ANA VCC - KIOSCO (ZAPALA)",
                                          "customer_rank": 1})
        # ... and another one by the shop's name only
        cls.por_comercio = Partner.create({"name": "GIRASOL VCC DESPENSA", "customer_rank": 1})
        cls.otro = Partner.create({"name": "FERRETERIA NADA QUE VER V149", "customer_rank": 1})
        cls.Promise = cls.env["distri.promised.transfer"].sudo()

    def _ledger(self):
        self.env.flush_all()
        return self.Promise._distri_ledger_fingerprint()

    def _data(self, **extra):
        data = {"renglones": json.dumps([{"id": self.plantilla.id, "cantidad": 1}]),
                "nombre": PERSONA, "comercio": COMERCIO, "telefono": "299 412 3456",
                "domicilio": "Calle V149 1", "entrega": "domicilio"}
        data.update(extra)
        return data

    def _order(self, nombre=PERSONA):
        return self.env["sale.order"].sudo().search([("distri_nombre", "=", nombre)],
                                                    order="id desc", limit=1)

    # ------------------------------------------------------------ the form

    def test_form_asks_the_person_first_and_the_shop_optional(self):
        html = self.url_open("/pedido").text
        nombre = html.index('name="nombre"')
        comercio = html.index('name="comercio"')
        self.assertLess(html.index("Nombre y apellido"), nombre)
        self.assertLess(nombre, comercio, "the person's name comes first")
        self.assertRegex(html, r'name="nombre" id="f_nombre"[^>]*autocomplete="name"')
        self.assertRegex(html, r'name="comercio" id="f_comercio"[^>]*autocomplete="organization"')
        self.assertIn("Nombre del comercio <span class=\"opcional\">(si tenés)</span>", html)
        self.assertIn("Poné tu nombre y apellido", html)
        # only the name is checked by the page; the shop has no error line
        self.assertIn("var pares = [['f_nombre', 'e_nombre']];", html)
        self.assertNotIn("e_comercio", html)
        self.assertNotIn('name="comercio" id="f_comercio" class="dato" required', html)
        # the encomienda block as it was
        self.assertIn("Nombre completo de quien retira", html)
        # the steps in the browser history and the device draft
        self.assertIn("pedido_pasos.js", html)
        self.assertIn("DNPedidoPasos.crear(window, mostrar)", html)
        self.assertEqual(self.url_open("/dn/pedido_pasos.js").status_code, 200)

    def test_name_required_shop_optional_both_saved(self):
        before = self._ledger()
        res = post_pedido(self, self._data(nombre="  "))
        self.assertEqual(res.status_code, 400)
        self.assertIn("poné tu nombre y apellido.", res.text)
        self.assertFalse(self._order(""))
        # without a shop: fine
        res = post_pedido(self, self._data(nombre="Solo Persona V149", comercio=""))
        self.assertEqual(res.status_code, 200, res.text[:300])
        solo = self._order("Solo Persona V149")
        self.assertTrue(solo)
        self.assertFalse(solo.distri_comercio)
        self.assertEqual(solo._distri_nombre_web(), "Solo Persona V149")
        self.assertNotIn("Comercio:", solo.note)
        # with both
        res = post_pedido(self, self._data(comercio="  Almacén   Girasol Vcc "))
        self.assertEqual(res.status_code, 200, res.text[:300])
        order = self._order()
        self.assertEqual(order.distri_nombre, PERSONA)
        self.assertEqual(order.distri_comercio, COMERCIO)
        self.assertIn("Nombre: %s\nComercio: %s\nTelefono:" % (PERSONA, COMERCIO), order.note)
        self.assertTrue(order.partner_id.distri_generic_partner)
        # the confirmation's WhatsApp text: «Nombre Apellido · Comercio»
        self.assertIn("*Cliente:* %s · %s" % (PERSONA, COMERCIO), unquote(res.text))
        # and it clears the device draft of this visitor
        self.assertRegex(res.text, r'data-borrador="visitante:\d+">try\{localStorage.removeItem')
        self.assertEqual(self._ledger(), before)

    def test_edit_by_link_prefills_both(self):
        post_pedido(self, self._data())
        order = self._order()
        page = "/pedido/%s/modificar" % order.access_token
        html = self.url_open(page).text
        self.assertRegex(html, r'name="nombre" id="f_nombre"[^>]*value="%s"' % re.escape(PERSONA))
        self.assertRegex(html, r'name="comercio" id="f_comercio"[^>]*value="%s"' % re.escape(COMERCIO))
        self.assertIn("Confirmá tus datos", html)
        # changing the shop through the link keeps the order's name
        res = post_pedido(self, self._data(comercio="Girasol Nuevo V149", token=order.access_token),
                          page=page)
        self.assertIn(res.status_code, (200, 303), res.text[:300])
        order.invalidate_recordset()
        self.assertEqual(order.distri_comercio, "Girasol Nuevo V149")
        self.assertEqual(order.distri_nombre, PERSONA)

    def test_staff_path_shows_both_fields_and_saves_them(self):
        self.authenticate(self.vendedor.login, self.vendedor.login)
        html = self.url_open("/pedido").text
        self.assertIn("Datos del cliente", html)
        self.assertLess(html.index('name="nombre"'), html.index('name="comercio"'))
        res = post_pedido(self, self._data(nombre="Persona Staff V149", comercio="Kiosco Staff V149"))
        self.assertEqual(res.status_code, 200, res.text[:300])
        order = self._order("Persona Staff V149")
        self.assertEqual(order.distri_comercio, "Kiosco Staff V149")
        self.assertEqual(order.user_id, self.vendedor)
        # the seller's confirmation names the buyer with both
        self.assertIn("Persona Staff V149 · Kiosco Staff V149", unquote(res.text))

    # ------------------------------------------------------------ assigning

    def test_assignment_finds_the_customer_by_person_or_by_shop(self):
        post_pedido(self, self._data())
        order = self._order()
        before = self._ledger()
        wizard = self.env["distri.asignar.cliente"].with_user(self.admin).with_context(
            default_order_id=order.id).create({})
        self.assertEqual(wizard.web_comercio, COMERCIO)
        self.assertIn(self.por_persona, wizard.candidato_ids, "found by the person's name")
        self.assertIn(self.por_comercio, wizard.candidato_ids, "found by the shop's name")
        self.assertNotIn(self.otro, wizard.candidato_ids)
        marks = {line.partner_id: line for line in wizard.linea_ids}
        self.assertTrue(marks[self.por_persona].nombre.startswith("✓ "))
        self.assertTrue(marks[self.por_comercio].nombre.startswith("✓ "))
        # a new customer starts as «NOMBRE - COMERCIO»
        self.assertEqual(wizard.nuevo_nombre, "%s - %s" % (PERSONA, COMERCIO))
        # only the shop typed in the box (an order with the person's name
        # spelled differently) still finds the shop's customer
        Partner = self.env["res.partner"]
        found = Partner._distri_candidatos_pedido("Otra Cosa", "Nadie Conocido", "",
                                                  comercio_web="Girasol Vcc")
        self.assertIn(self.por_comercio, found)
        found = Partner._distri_candidatos_pedido("", "Pérez Ana Vcc", "")
        self.assertIn(self.por_persona, found, "accents and word order do not matter")
        self.assertEqual(self._ledger(), before)

    def test_staff_screens_show_both(self):
        post_pedido(self, self._data())
        order = self._order()
        both = "%s · %s" % (PERSONA, COMERCIO)
        self.assertEqual(order._distri_nombre_cliente(), both)
        self.assertIn("*Cliente:* %s" % both, order._distri_company_message_default())
        self.assertTrue(order._distri_wa_reminder_text().startswith("Hola %s," % PERSONA))
        self.authenticate(self.admin.login, self.admin.login)
        html = self.url_open("/revisar/%s/asignar" % order.id).text
        self.assertIn(both, html)
        self.assertIn("Nombre del cliente", html)
        self.assertIn("%s - %s" % (PERSONA, COMERCIO), html)
        # «Pedidos para revisar»: the card of the order
        self.assertEqual(card(order)["cliente"], both)
        # the backend: «Nombre y apellido» and «Comercio»
        arch = self.env.ref("distri_pedido.view_order_list_sin_whatsapp").arch_db
        self.assertIn('name="distri_comercio" string="Comercio"', arch)
        self.assertIn('name="distri_nombre" string="Nombre y apellido"', arch)
        self.assertEqual(self.env["sale.order"]._fields["distri_nombre"].string,
                         "Nombre y apellido (web)")

    def test_old_orders_keep_their_single_name(self):
        post_pedido(self, self._data(nombre="Kiosco Viejo V149", comercio=""))
        order = self._order("Kiosco Viejo V149")
        self.assertEqual(order._distri_nombre_cliente(), "Kiosco Viejo V149")
        self.assertEqual(nombre_web("Ana", "ana"), "Ana")
        self.assertEqual(nombre_web("", "Kiosco"), "Kiosco")
        self.assertEqual(nombre_web(" Ana  B ", " Kiosco "), "Ana B · Kiosco")
        self.assertEqual(nombre_web("Ana", "Kiosco", sep=" - "), "Ana - Kiosco")

    # ------------------------------------------------------------ the JS (node)

    def _node(self, script):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data", script), root],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_steps_history_and_draft_in_node(self):
        checks = self._node("pedido_v129_49.js")
        failed = ["%s.%s" % (group, name) for group, values in checks.items()
                  for name, ok in values.items() if ok is not True]
        self.assertFalse(failed, checks)
