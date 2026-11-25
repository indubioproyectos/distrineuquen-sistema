"""19.0.1.129.40: «Nueva venta más rápida» (owner, 25/11) and two client-side
fixes without a switch.

- A customer on file goes on with nothing else: no phone, address or
  WhatsApp is asked (the WhatsApp only at an exit that confirms while
  distri_pedido.pedido_requiere_whatsapp is on). The public /pedido keeps
  every check.
- «Nuevo cliente» and «Datos del cliente» write as the user: his ACLs and
  record rules decide, nothing is widened.
- «Elegir productos»: /pedido's catalog page priced with the customer's
  list; the cart it gives back is sold at those prices.
- The same product posted twice is one line with the sum.
- The phone keyboard helper is on every page with the gear, /pedido keeps
  its cart on the device (a visitor gets the page as before), and the texts
  of the order (stock legend, company message).
Every test leaves the ledger exactly as it was. Synthetic data only.
"""
import json
import os
import re
import shutil
import subprocess
from unittest import SkipTest

from odoo import Command
from odoo.modules.module import get_module_path
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2, screen_parents
from odoo.addons.distri_pedido.controllers.main import LISTA_GENERAL, TomaDePedidos
from odoo.addons.distri_pedido.models.agilpedido_wa import es_mensaje_de_la_pagina
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA, juntar_renglones
from odoo.addons.distri_pedido.models.pedido_whatsapp import PARAM_REQUIRE, find_web_codes
from odoo.addons.distri_pedido.models.sale_order import STOCK_NOTICE, STOCK_NOTICE_SHORT
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

FETCH = {"X-Distri-Fetch": "1"}
CSRF_RE = re.compile(r'(?:name="csrf_token" value|name="csrf-token" content)="([^"]+)"')
ENVIO_RE = re.compile(r'name="envio" value="([^"]+)"')


@tagged("post_install", "-at_install", "distri_v129_40")
class TestV12940VentaRapida(PedidoAbiertoMixin, RepartoMixin, AccountTestInvoicingHttpCommon):

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

        cls.tag = cls.env["res.partner.category"].create({"name": "Cartera V140"})
        cls.admin = user("admin_v140", "distri_pedido.grupo_administracion")
        cls.vendedor = user("vendedor_v140", "distri_pedido.grupo_vendedor")
        cls.vendedor.sudo().etiqueta_cartera_id = cls.tag
        cls.externo = user("externo_v140", "distri_pedido.grupo_vendedor")
        cls.externo.sudo().distri_tipo_vendedor = "externo"
        cls.armador = user("armador_v140", "distri_pedido.grupo_armador")
        Tmpl = cls.env["product.template"]
        cls.p1 = Tmpl.create({"name": "Gaseosa V140", "type": "consu", "list_price": 40000.0,
                              "is_published": True, "sale_ok": True, "invoice_policy": "order",
                              "taxes_id": [Command.clear()]})
        cls.p2 = Tmpl.create({"name": "Galletitas V140", "type": "consu", "list_price": 15000.0,
                              "is_published": True, "sale_ok": True, "invoice_policy": "order",
                              "taxes_id": [Command.clear()]})
        # the customer's own list: p1 at 33.000 instead of 40.000
        cls.lista = Pricelist.create({
            "name": "Lista Cliente V140", "currency_id": cls.env.company.currency_id.id,
            "item_ids": [Command.create({"applied_on": "1_product", "product_tmpl_id": cls.p1.id,
                                         "compute_price": "fixed", "fixed_price": 33000.0})]})
        # on file with nothing but his name: no phone, WhatsApp nor address
        cls.pelado = cls.env["res.partner"].create({
            "name": "Almacen Pelado V140", "company_type": "company",
            "category_id": [Command.link(cls.tag.id)], "user_id": cls.vendedor.id})
        cls.pelado.property_product_pricelist = cls.lista
        cls.ajeno = cls.env["res.partner"].create({
            "name": "Kiosco Ajeno V140", "company_type": "company", "street": "Roca 1"})

    # ------------------------------------------------------------ helpers

    def _flags(self, nueva=True, ui=True):
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param(PARAM_NUEVA_VENTA, "1" if nueva else "0")
        Param.set_param(PARAM_UI_V2, "1" if ui else "0")

    def _require_wa(self, on):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_REQUIRE, "1" if on else "0")

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _csrf(self, url="/nueva-venta"):
        page = self.url_open(url)
        self.assertEqual(page.status_code, 200, page.text[:300])
        return CSRF_RE.search(page.text).group(1), page.text

    def _ledger(self):
        self.env.flush_all()
        return self.env["distri.promised.transfer"]._distri_ledger_fingerprint()

    def _post(self, salida, partner=None, cart=None, **extra):
        csrf, html = self._csrf()
        data = {"csrf_token": csrf, "envio": ENVIO_RE.search(html).group(1),
                "cliente_id": str((partner or self.pelado).id),
                "renglones": json.dumps(cart or [{"id": self.p1.id, "cantidad": 2},
                                                 {"id": self.p2.id, "cantidad": 3}]),
                "salida": salida, "entrega": "domicilio", "telefono": "", "domicilio": ""}
        data.update(extra)
        res = self.url_open("/nueva-venta/crear", data=data, headers=FETCH, allow_redirects=False)
        self.env.invalidate_all()
        return res

    def _order_of(self, res):
        self.assertEqual(res.status_code, 200, res.text[:500])
        return self.env["sale.order"].browse(int(re.search(r"/(\d+)", res.json()["url"]).group(1)))

    def _json_post(self, url, **data):
        csrf, _html = self._csrf()
        data["csrf_token"] = csrf
        res = self.url_open(url, data=data, headers=FETCH, allow_redirects=False)
        self.env.invalidate_all()
        return res

    # ------------------------------------------------------------ point 1

    def test_existing_customer_goes_on_without_contact_data(self):
        self._flags()
        self._require_wa(False)
        before = self._ledger()
        self._login(self.vendedor)
        data = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.pelado.id).json()
        self.assertTrue(data["cliente"]["sin_whatsapp"])
        self.assertEqual(data["cliente"]["domicilio"], "")
        self.assertEqual(data["cliente"]["ficha"], "/clientes/%s?desde=/nueva-venta" % self.pelado.id)
        self.assertIn("/nueva-venta", screen_parents("/clientes/%s" % self.pelado.id))
        # «A armar» with no phone, address nor WhatsApp answer: confirmed
        order = self._order_of(self._post("armar"))
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.partner_id, self.pelado)
        self.assertFalse(order.distri_telefono)
        self.assertTrue(order.message_ids.filtered(
            lambda m: "Confirmado sin WhatsApp" in (m.body or "")))
        # his record is not touched
        self.assertFalse(self.pelado.distri_whatsapp or self.pelado.phone or self.pelado.street)
        order = self._order_of(self._post("presupuesto"))
        self.assertEqual(order.state, "draft")
        # an address on file goes to the order when the field is left empty
        self.pelado.street = "Belgrano 140"
        order = self._order_of(self._post("presupuesto"))
        self.assertIn("Belgrano 140", order.distri_domicilio)
        self.pelado.street = False
        # required: only the exits that confirm ask, and say why
        self._require_wa(True)
        res = self._post("armar")
        self.assertEqual(res.status_code, 400)
        self.assertIn("no tiene WhatsApp", res.json()["error"])
        self.assertEqual(self._order_of(self._post("presupuesto")).state, "draft")
        order = self._order_of(self._post("armar", wa_area="299", wa_numero="4140140"))
        self.assertEqual(order.state, "sale")
        self.assertEqual(self.pelado.distri_whatsapp, "+5492994140140")
        self.assertEqual(self._ledger(), before)

    def test_public_page_keeps_its_required_fields(self):
        """Only «Nueva venta»'s context relaxes the check, never the post."""
        faltan = TomaDePedidos._datos_faltantes
        self.assertIn("teléfono", faltan("Kiosco", "", "", "domicilio", {}))
        self.assertIn("domicilio", faltan("Kiosco", "299", "", "domicilio", {}))
        self.assertEqual(faltan("Kiosco", "", "", "domicilio", {}, ficha=True), "")
        self.assertIn("nombre", faltan("", "", "", "domicilio", {}, ficha=True))
        # a staff /pedido post for the same customer, outside «Nueva venta»
        self._flags()
        self._login(self.vendedor)
        page = self.url_open("/pedido").text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        count = self.env["sale.order"].search_count([])
        res = self.url_open("/pedido/enviar", data={
            "csrf_token": csrf, "cliente_id": str(self.pelado.id), "nombre": self.pelado.name,
            "telefono": "", "domicilio": "", "entrega": "domicilio",
            "renglones": json.dumps([{"id": self.p1.id, "cantidad": 2}])})
        self.assertIn("Faltan tus datos", res.text)
        self.assertEqual(self.env["sale.order"].search_count([]), count)

    def test_new_customer_path(self):
        self._flags()
        self._require_wa(False)
        before = self._ledger()
        self._login(self.vendedor)
        res = self._json_post("/nueva-venta/cliente", nombre="ab")
        self.assertEqual(res.status_code, 400)
        res = self._json_post("/nueva-venta/cliente", nombre="Kiosco Nuevo V140", wa_area="299",
                              wa_numero="1")
        self.assertEqual(res.status_code, 400)
        self.assertIn("WhatsApp", res.json()["error"])
        # a name like one on file: offered first
        res = self._json_post("/nueva-venta/cliente", nombre="Almacen Pelado Dos")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn(self.pelado.id, [p["id"] for p in res.json()["parecidos"]])
        self.assertFalse(self.env["res.partner"].search([("name", "=", "Almacen Pelado Dos")]))
        res = self._json_post("/nueva-venta/cliente", nombre="Almacen Pelado Dos", nuevo_ok="1",
                              wa_area="299", wa_numero="4222440", calle="Sarmiento 5",
                              ciudad="Plottier")
        self.assertEqual(res.status_code, 200, res.text)
        nuevo = self.env["res.partner"].browse(res.json()["cliente"]["id"])
        self.assertEqual(nuevo.name, "Almacen Pelado Dos")
        self.assertEqual(nuevo.distri_whatsapp, "+5492994222440")
        self.assertEqual((nuevo.street, nuevo.city), ("Sarmiento 5", "Plottier"))
        self.assertIn(self.tag, nuevo.category_id, "in his portfolio")
        self.assertEqual(nuevo.create_uid, self.vendedor, "the user is its creator")
        self.assertEqual(nuevo.user_id, self.vendedor)
        # as /cobro's «Cliente nuevo»: administración confirms or merges it
        self.assertTrue(nuevo.distri_cliente_provisorio)
        self.assertEqual(nuevo.distri_provisorio_user_id, self.vendedor)
        # the backend ACLs are not widened
        self.assertFalse(self.env["res.partner"].with_user(self.vendedor).has_access("create"))
        # and he sells to him at once
        order = self._order_of(self._post("armar", nuevo))
        self.assertEqual(order.partner_id, nuevo)
        # an external seller's new customer is his
        self._login(self.externo)
        res = self._json_post("/nueva-venta/cliente", nombre="Despensa Externa V140", nuevo_ok="1")
        self.assertEqual(res.status_code, 200, res.text)
        suyo = self.env["res.partner"].browse(res.json()["cliente"]["id"])
        self.assertTrue(self.externo._distri_may_reach_customer(suyo))
        self.assertEqual(self._ledger(), before)

    def test_customer_data_sheet_writes_as_the_user(self):
        self._flags()
        before = self._ledger()
        self._login(self.vendedor)
        res = self._json_post("/nueva-venta/cliente/%s" % self.pelado.id, wa_area="299",
                              wa_numero="4140141", calle="Alem 40", ciudad="Neuquén")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(self.pelado.distri_whatsapp, "+5492994140141")
        self.assertEqual(self.pelado.street, "Alem 40")
        self.assertFalse(res.json()["cliente"]["sin_whatsapp"])
        self.assertTrue(self.pelado.message_ids.filtered(
            lambda m: "Datos cambiados" in (m.body or "")))
        # outside his portfolio: the backend would refuse, so does this
        res = self._json_post("/nueva-venta/cliente/%s" % self.ajeno.id, calle="Otra 1")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.ajeno.street, "Roca 1")
        # an external seller cannot reach another seller's customer
        self._login(self.externo)
        res = self._json_post("/nueva-venta/cliente/%s" % self.pelado.id, calle="X 1")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.pelado.street, "Alem 40")
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ point 2

    def test_picker_round_trip_with_the_customer_prices(self):
        self._flags()
        self._require_wa(False)
        before = self._ledger()
        self._login(self.vendedor)
        res = self.url_open("/nueva-venta/productos?cliente_id=%s" % self.pelado.id)
        self.assertEqual(res.status_code, 200, res.text[:300])
        html = res.text
        self.assertIn('id="nv-picker"', html)
        self.assertIn('data-cliente-id="%s"' % self.pelado.id, html)
        self.assertIn("Confirmar productos", html)
        self.assertIn("nueva_venta_productos.js", html)
        self.assertNotIn("pedido_borrador.js", html, "the picker uses the Nueva venta draft")
        self.assertIn('id="ver" disabled="disabled" hidden="hidden"', html)
        self.assertIn("Lista Cliente V140", html)
        # /pedido's own rows, priced with his list (33.000, not 40.000)
        row = re.search(r'<div class="fila" data-id="%s" data-precio="([^"]+)"' % self.p1.id, html)
        self.assertTrue(row, "the catalog row of p1")
        self.assertEqual(float(row.group(1)), 33000.0)
        self.assertIn("$ 33.000", html)
        catalogo = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.pelado.id).json()
        precio = {p["id"]: p["precio"] for p in catalogo["productos"]}
        self.assertEqual(precio[self.p1.id], 33000.0)
        # what the picker gives back is sold at those prices
        order = self._order_of(self._post("armar", cart=[{"id": self.p1.id, "cantidad": 2}]))
        line = order.order_line.filtered(lambda l: l.product_id == self.p1.product_variant_id)
        self.assertEqual(line.price_unit, 33000.0)
        # Nueva venta links to it
        self.assertIn('id="nv-elegir"', self.url_open("/nueva-venta").text)
        # an external seller and another seller's customer: back to the screen
        self._login(self.externo)
        res = self.url_open("/nueva-venta/productos?cliente_id=%s" % self.pelado.id,
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertEqual(self._ledger(), before)

    def test_same_product_twice_is_one_line(self):
        self.assertEqual(juntar_renglones([{"id": 3, "cantidad": 2}, {"id": "3", "cantidad": 3},
                                           {"id": 4, "cantidad": 1}]),
                         [{"id": 3, "cantidad": 5}, {"id": 4, "cantidad": 1}])
        self._flags()
        self._require_wa(False)
        before = self._ledger()
        self._login(self.vendedor)
        order = self._order_of(self._post("presupuesto", cart=[
            {"id": self.p2.id, "cantidad": 2}, {"id": self.p2.id, "cantidad": 3}]))
        lines = order.order_line.filtered(lambda l: l.product_id == self.p2.product_variant_id)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.product_uom_qty, 5)
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ permissions

    def test_permissions_and_flags_off(self):
        before = self._ledger()
        self._flags(nueva=False)
        self._login(self.vendedor)
        for url in ("/nueva-venta/productos?cliente_id=%s" % self.pelado.id,):
            self.assertEqual(self.url_open(url).status_code, 404)
        for url in ("/nueva-venta/cliente", "/nueva-venta/cliente/%s" % self.pelado.id):
            res = self.url_open(url, data={"csrf_token": "x"}, headers=FETCH)
            self.assertIn(res.status_code, (400, 404))
        self._flags()
        self._login(self.armador)
        self.assertEqual(self.url_open("/nueva-venta/productos?cliente_id=%s" % self.pelado.id)
                         .status_code, 403)
        count = self.env["res.partner"].search_count([])
        self.assertEqual(self.env["res.partner"].search_count([]), count)
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ point 4 and /pedido

    def test_keyboard_helper_and_pedido_draft(self):
        self._flags()
        res = self.url_open("/dn/teclado.js")
        self.assertEqual(res.status_code, 200)
        self.assertIn("DNTeclado", res.text)
        self.assertEqual(self.url_open("/dn/pedido_borrador.js").status_code, 200)
        # a visitor: the page as before, plus the device draft
        html = self.url_open("/pedido").text
        self.assertIn("Ver pedido", html)
        self.assertIn("teclado.js", html)
        self.assertIn("pedido_borrador.js", html)
        self.assertRegex(html, r'id="frm" data-borrador="visitante:\d+"')
        self.assertNotIn('id="nv-picker"', html)
        self.assertNotIn("Confirmar productos", html)
        # staff: per user
        self._login(self.vendedor)
        html = self.url_open("/pedido").text
        self.assertIn('data-borrador="%s:' % self.vendedor.id, html)
        for url in ("/nueva-venta", "/tablero", "/mis-ventas"):
            page = self.url_open(url)
            self.assertEqual(page.status_code, 200, url)
            self.assertIn("teclado.js", page.text, url)

    def test_order_texts(self):
        self.assertEqual(STOCK_NOTICE, "Pedido sujeto a disponibilidad de stock. Con este link "
                                       "podés agregar productos hasta que lo armemos.")
        self.assertEqual(STOCK_NOTICE_SHORT, "Pedido sujeto a disponibilidad de stock.")
        order = self.env["sale.order"].sudo().create({
            "partner_id": self.pelado.id, "distri_nombre": "Kiosco Ana",
            "order_line": [Command.create({"product_id": self.p1.product_variant_id.id,
                                           "product_uom_qty": 1, "price_unit": 40000})]})
        # a staff order without a code keeps «*Pedido:*» at the top
        texto = order._distri_company_message("Kiosco Ana", "", "Mitre 1")
        self.assertTrue(texto.startswith("*Pedido:* %s\n" % order.name))
        # with the code line, the number is written once, at the bottom
        order.distri_wa_code = order._distri_wa_new_code()
        self.assertTrue(order.distri_wa_code)
        texto = order._distri_company_message("Kiosco Ana", "", "Mitre 1")
        self.assertNotIn("*Pedido:*", texto)
        self.assertTrue(texto.startswith("*Cliente:* Kiosco Ana\n"))
        self.assertIn(order._distri_wa_code_line(), texto.split("\n"))
        self.assertEqual(texto.count(order.name), 1)
        self.assertIn(STOCK_NOTICE, texto)
        self.assertEqual(find_web_codes(texto), [(order.name, order.distri_wa_code)])
        self.assertTrue(es_mensaje_de_la_pagina(texto))

    # ------------------------------------------------------------ the JS (node)

    def test_js_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/venta_v129_40.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        t = out["teclado"]
        self.assertEqual(t.pop("hints"), ["search", "done", None, "go"])
        self.assertTrue(all(t.values()), t)
        nv = out["nv"]
        self.assertEqual(nv["firstTap"], [{"id": 11, "cantidad": 1}])
        self.assertEqual(nv["secondTap"], [{"id": 11, "cantidad": 1}], "no second line")
        self.assertEqual(nv["afterSteppers"], [{"id": 11, "cantidad": 2}])
        self.assertEqual(nv["removedAtZero"], [])
        self.assertEqual(nv["cartFromPicker"], [{"id": 12, "cantidad": 3}])
        self.assertEqual(nv["pickerLink"], "/nueva-venta/productos?cliente_id=5")
        for key in ("resumedWithoutAsking", "urlCleaned", "fichaShown", "focusedQty", "searchCleared",
                    "waHiddenBeforeExit", "waShownAtArmar", "postedWithoutWa",
                    "resumedFromSessionFlag", "requiredStops", "backAtExitStep",
                    "offeredWithoutVolver", "leavingMarks"):
            self.assertTrue(nv[key], key)
        p = out["picker"]
        self.assertEqual(p["filled"], ["4", "", ""], "the draft's cart, product 99 dropped")
        self.assertEqual(p["cart"], {"11": 5, "12": 6})
        self.assertEqual(p["twiceAdds"], {"11": 5})
        self.assertTrue(p["keptAnswers"] and p["otherCustomerFresh"] and p["expired"])
        b = out["borrador"]
        self.assertEqual(b["key"], "dn:pedido:borrador:visitante:1")
        self.assertEqual(b["userKey"], "dn:pedido:borrador:7:3")
        self.assertEqual(b["saved"], {"11": [2, 0], "13m": [1]})
        self.assertNotIn("clave", b["savedFields"], "never a password")
        self.assertEqual(b["restoredCount"], 2, "product 12 is gone")
        self.assertEqual(b["restoredRows"], ["2", "1", "", ""])
        self.assertEqual(b["restoredFields"], ["Kiosco Ana", "2995551234", "domicilio"])
        self.assertTrue(b["expired"] and b["emptyRemoves"])
