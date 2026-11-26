"""19.0.1.98.0: /armado «Tradicional»/«Tarjetas» and «Alfabético»/«Por
secciones» (per-user preference), natural sort, the depot walking order,
the offline queue replayed idempotently, opening takes the order, «Más
tarde» keeps the marks, the customer comment / depot note / «Mi vendedor
es» from /pedido to the order, the packer and the driver, and the «Ajustes»
gear (with «Salir») on every custom page. Synthetic data only."""
import json
import re
import uuid

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.agil_tienda import PARAM_CATALOGO_AGIL
from odoo.addons.distri_pedido.models.armado import clave_natural
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

FETCH = {"X-Distri-Fetch": "1"}
CSRF = re.compile(r'name="csrf-token" content="([^"]+)"')


def _uuid():
    return str(uuid.uuid4())


@tagged("post_install", "-at_install", "distri_armado_v98")
class TestArmadoV98(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        groups = "base.group_user,distri_pedido.grupo_armador"
        cls.packer = new_test_user(cls.env, login="armador_v98", name="Armador V98", groups=groups,
                                   company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.other = new_test_user(cls.env, login="armador_v98_b", name="Otro V98", groups=groups,
                                  company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.partner = cls.env["res.partner"].create({"name": "X - KIOSCO V98", "city": "Centenario"})
        Cat = cls.env["product.category"]
        cls.cat_bebidas = Cat.create({"name": "Bebidas V98"})
        cls.cat_golosinas = Cat.create({"name": "Golosinas V98"})
        cls.cat_cigarrillos = Cat.create({"name": "Cigarrillos V98"})

        def product(name, categ):
            return cls.env["product.product"].create({
                "name": name, "list_price": 10, "type": "consu", "categ_id": categ.id,
                "taxes_id": [Command.clear()]})
        cls.golden20 = product("Golden 20", cls.cat_cigarrillos)
        cls.golden10 = product("golden 10", cls.cat_cigarrillos)
        cls.agua = product("Ágüa mineral 2", cls.cat_bebidas)
        cls.caramelo = product("Caramelo Flynn Paff", cls.cat_golosinas)
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "V98", "company_id": company.id, "currency_id": company.currency_id.id})

    def setUp(self):
        super().setUp()
        self.order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "pricelist_id": self.pricelist.id,
            "distri_comentario": "Recibe de 10 a 13 h",
            "distri_nota_deposito": "PARA MAÑANA",
            "order_line": [Command.create({"product_id": p.id, "product_uom_qty": q, "price_unit": 10,
                                           "tax_ids": [Command.clear()]})
                           for p, q in ((self.golden20, 6), (self.caramelo, 12), (self.golden10, 3),
                                        (self.agua, 2))],
        })
        self.order.action_confirm()
        self.line = {l.product_id: l for l in self.order.order_line}
        self.url = "/armado/pedido/%s" % self.order.id

    def _open(self, login="armador_v98"):
        self.authenticate(login, login)
        page = self.url_open(self.url)
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.csrf = CSRF.search(page.text).group(1)
        return page

    def _post(self, url, **data):
        data["csrf_token"] = self.csrf
        return self.url_open(url, data=data, headers=FETCH, allow_redirects=False)

    def _sync(self, eventos):
        res = self._post(self.url + "/sync", eventos=json.dumps(eventos))
        self.assertEqual(res.status_code, 200, res.text[:300])
        return res.json()

    # ------------------------------------------------- order and views

    def test_natural_sort_key(self):
        nombres = ["Golden 20", "golden 10", "Golden 9", "Ácido 5", "acido 40", "Zeta"]
        self.assertEqual(sorted(nombres, key=clave_natural),
                         ["Ácido 5", "acido 40", "Golden 9", "golden 10", "Golden 20", "Zeta"])

    def test_alphabetical_and_sections(self):
        secciones = self.order._distri_renglones_ordenados("alfabetico")
        self.assertEqual(len(secciones), 1)
        self.assertEqual(secciones[0]["renglones"].mapped("product_id.name"),
                         ["Ágüa mineral 2", "Caramelo Flynn Paff", "golden 10", "Golden 20"])
        # default walking order: every section 0 -> alphabetical by section
        nombres = [s["nombre"] for s in self.order._distri_renglones_ordenados("secciones")]
        self.assertEqual(nombres, ["Bebidas V98", "Cigarrillos V98", "Golosinas V98"])
        # administración drags the sections: the depot walking order wins
        self.cat_golosinas.distri_recorrido = 1
        self.cat_cigarrillos.distri_recorrido = 2
        self.cat_bebidas.distri_recorrido = 3
        secciones = self.order._distri_renglones_ordenados("secciones")
        self.assertEqual([s["nombre"] for s in secciones], ["Golosinas V98", "Cigarrillos V98", "Bebidas V98"])
        self.assertEqual(secciones[1]["renglones"].mapped("product_id.name"), ["golden 10", "Golden 20"])

    def test_preference_defaults_and_toggle(self):
        self.assertEqual((self.packer.distri_armado_vista, self.packer.distri_armado_orden),
                         ("tradicional", "alfabetico"))
        page = self._open()
        self.assertIn("arm-trad", page.text)
        self.assertIn('data-page="trad"', page.text)
        # quantity first, the pinned comment and note
        self.assertRegex(page.text, r'tl__cant dn-num">6</span>\s*<span class="tl__nom">Golden 20')
        self.assertIn("Recibe de 10 a 13 h", page.text)
        self.assertIn("PARA MAÑANA", page.text)
        res = self._post("/armado/preferencias", vista="tarjetas", orden="secciones")
        self.assertEqual(res.json(), {"ok": True, "vista": "tarjetas", "orden": "secciones"})
        self.assertEqual((self.packer.distri_armado_vista, self.packer.distri_armado_orden),
                         ("tarjetas", "secciones"))
        page = self.url_open(self.url).text
        self.assertIn('data-page="pedido"', page)
        self.assertIn("Listo, 6", page)
        self.assertIn('class="dn-label arm-sec">Bebidas V98', page)
        # a bad value changes nothing; without fetch it goes back
        res = self.url_open("/armado/preferencias", data={"csrf_token": self.csrf, "vista": "x",
                                                          "volver": self.url}, allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith(self.url))
        self.assertEqual(self.packer.distri_armado_vista, "tarjetas")

    def test_opening_takes_the_order(self):
        self.assertEqual(self.order.distri_armado, "pendiente")
        page = self._open()
        self.assertEqual((self.order.distri_armado, self.order.distri_armador_id), ("en_armado", self.packer))
        self.assertIn("Lo tomaste", page.text)
        self.assertIn("Todo listo", page.text)
        # someone else opening it does not steal it
        self._open("armador_v98_b")
        self.assertEqual(self.order.distri_armador_id, self.packer)

    def test_list_shows_amount_note_and_small_orders(self):
        self.order.order_line.filtered(lambda l: l.product_id == self.agua).product_uom_qty = 0
        self._open()
        lista = self.url_open("/armado").text
        self.assertIn("PARA MAÑANA", lista)
        self.assertIn("Cliente: Recibe de 10 a 13 h", lista)
        self.assertIn("$ 210", lista)
        # 3 lines or fewer: the whole order is shown in the list
        self.assertIn('class="arm-ped__mini"', lista)

    # ------------------------------------------------- offline queue

    def test_offline_queue_replays_idempotently(self):
        self._open()
        g20, car = self.line[self.golden20], self.line[self.caramelo]
        tick, hay = _uuid(), _uuid()
        res = self._sync([
            {"uuid": tick, "tipo": "listo", "linea": car.id, "valor": True},
            {"uuid": hay, "tipo": "hay", "linea": g20.id, "valor": 4},
        ])
        self.assertEqual([r["status"] for r in res["resultados"]], ["ok", "ok"])
        self.assertTrue(car.distri_listo)
        self.assertEqual((g20.distri_faltante, g20.product_uom_qty), (2, 4))
        # the packer untaps the line; then the phone resends the first batch
        # (its answer was lost): nothing is applied twice, nothing undone
        self.assertEqual(self._sync([{"uuid": _uuid(), "tipo": "listo", "linea": car.id,
                                      "valor": False}])["resultados"][0]["status"], "ok")
        res = self._sync([
            {"uuid": tick, "tipo": "listo", "linea": car.id, "valor": True},
            {"uuid": hay, "tipo": "hay", "linea": g20.id, "valor": 4},
        ])
        self.assertEqual([r["status"] for r in res["resultados"]], ["duplicate", "duplicate"])
        self.assertFalse(car.distri_listo)
        self.assertEqual(self.env["distri.armado.evento"].sudo().search_count(
            [("pedido_id", "=", self.order.id)]), 3)
        # «están todas» takes the shortage back and counts as checked
        self._sync([{"uuid": _uuid(), "tipo": "hay", "linea": g20.id, "valor": 6}])
        self.assertEqual((g20.distri_faltante, g20.product_uom_qty, g20.distri_listo), (0, 6, True))
        # bad values are refused for good, the rest goes on
        res = self._sync([
            {"uuid": _uuid(), "tipo": "hay", "linea": g20.id, "valor": 9},
            {"uuid": "x", "tipo": "listo", "linea": g20.id, "valor": True},
            {"uuid": _uuid(), "tipo": "listo", "linea": 0, "valor": True},
        ])
        self.assertEqual([(r["status"], r.get("retry")) for r in res["resultados"]],
                         [("error", False)] * 3)
        # the finish, replayed
        fin = _uuid()
        res = self._sync([{"uuid": fin, "tipo": "terminar", "valor": 2}])
        self.assertEqual(res["resultados"][0]["status"], "ok")
        self.assertEqual(res["url"], "/armado?terminado=%s" % self.order.id)
        self.assertEqual((self.order.distri_armado, self.order.distri_bultos), ("armado", 2))
        self.assertEqual(self._sync([{"uuid": fin, "tipo": "terminar", "valor": 2}])
                         ["resultados"][0]["status"], "duplicate")
        # a new tick after the finish is refused (not retried)
        res = self._sync([{"uuid": _uuid(), "tipo": "listo", "linea": car.id, "valor": True}])
        self.assertEqual((res["resultados"][0]["status"], res["resultados"][0]["retry"]), ("error", False))
        self.assertIn("ya está armado", res["resultados"][0]["error"])

    def test_sync_refuses_another_packer_and_garbage(self):
        self.order.with_user(self.packer).sudo().distri_tomar()
        self._open("armador_v98_b")
        res = self._sync([{"uuid": _uuid(), "tipo": "listo", "linea": self.line[self.agua].id, "valor": True}])
        self.assertEqual(res["resultados"][0]["status"], "error")
        self.assertIn("Armador V98", res["resultados"][0]["error"])
        self.assertFalse(self.line[self.agua].distri_listo)
        res = self._post(self.url + "/sync", eventos="{no es json")
        self.assertEqual(res.status_code, 422)

    # ------------------------------------------------- «Más tarde»

    def test_mas_tarde_keeps_the_marks(self):
        self._open()
        g20, car, agua = self.line[self.golden20], self.line[self.caramelo], self.line[self.agua]
        self._sync([{"uuid": _uuid(), "tipo": "listo", "linea": car.id, "valor": True},
                    {"uuid": _uuid(), "tipo": "hay", "linea": g20.id, "valor": 5},
                    {"uuid": _uuid(), "tipo": "listo", "linea": agua.id, "valor": True}])
        res = self._post(self.url + "/mas-tarde")
        self.assertEqual(res.json(), {"ok": True, "url": "/armado?dejado=%s" % self.order.id})
        self.assertEqual(self.order.distri_armado, "pendiente")
        self.assertTrue(car.distri_listo)
        self.assertEqual((g20.distri_faltante, g20.product_uom_qty), (1, 5))
        self.assertIn("Queda lo marcado: 2 revisados y 1 con faltante.",
                      " ".join(str(m.body) for m in self.order.message_ids))
        # the customer changes a line afterwards: that line is checked again
        agua.write({"product_uom_qty": 4})
        self.assertFalse(agua.distri_listo)
        g20.write({"product_uom_qty": 8})
        self.assertEqual((g20.distri_faltante, g20.distri_pedida), (0, 8))
        self.assertTrue(car.distri_listo, "an untouched line keeps its tick")
        # the next packer goes on from there
        self.order.with_user(self.other).sudo().distri_tomar()
        self.assertTrue(car.distri_listo)

    # ------------------------------------------------- driver and gear

    def test_gear_and_logout_on_armado(self):
        self._open()
        page = self.url_open("/armado").text
        self.assertIn('id="dn-gear"', page)
        self.assertIn('href="/web/session/logout"', page)
        self.assertIn("Armador V98", page)
        # the armado view in «Ajustes»
        self.assertIn('action="/armado/preferencias"', page)


@tagged("post_install", "-at_install", "distri_armado_v98")
class TestRepartoV98(RepartoMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()

    def test_driver_stop_shows_comment_and_note(self):
        order = self._order(self.partner_a, self.product_a)
        order.write({"distri_comentario": "Recibe de 10 a 13 h", "distri_nota_deposito": "Cobrar todo"})
        payload = self._route(order).stop_ids._app_payload()
        self.assertEqual((payload["comment"], payload["depot_note"]), ("Recibe de 10 a 13 h", "Cobrar todo"))


@tagged("post_install", "-at_install", "distri_armado_v98")
class TestPedidoV98(PedidoAbiertoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plantilla = cls.env["product.template"].create({
            "name": "Producto Prueba V98", "list_price": 70000.0, "is_published": True,
            "sale_ok": True, "type": "consu", "taxes_id": [Command.clear()]})
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_CATALOGO_AGIL, "0")
        cls.vendedor = cls.env["distri.vendedor.web"].create({"name": "Vendedora V98", "sequence": 1})
        cls.inactivo = cls.env["distri.vendedor.web"].create({"name": "Ex V98", "active": False})

    def _data(self, **extra):
        data = {"renglones": json.dumps([{"id": self.plantilla.id, "cantidad": 1}]),
                "nombre": "Kiosco V98", "telefono": "2995550098",
                "domicilio": "Calle de ejemplo 98", "entrega": "domicilio"}
        data.update(extra)
        return data

    def _order(self):
        return self.env["sale.order"].sudo().search([("distri_nombre", "=", "Kiosco V98")],
                                                    order="id desc", limit=1)

    def test_page_offers_comment_and_sellers(self):
        page = self.url_open("/pedido").text
        self.assertIn('name="comentario"', page)
        self.assertIn("Mi vendedor es:", page)
        self.assertIn("Vendedora V98", page)
        self.assertIn("No sé / ninguno", page)
        self.assertNotIn("Ex V98", page)
        self.assertNotIn('name="nota_deposito"', page, "the depot note is for staff only")
        # the starting list is there, readable on the public page
        self.assertTrue(self.env.ref("distri_pedido.vendedor_web_italo").exists())

    def test_comment_and_seller_reach_the_order_and_the_customer(self):
        res = post_pedido(self, self._data(comentario="  Recibimos de 9 a 13 h  ",
                                           vendedor_web_id=str(self.vendedor.id)))
        self.assertEqual(res.status_code, 200, res.text[:300])
        pedido = self._order()
        self.assertEqual(pedido.distri_comentario, "Recibimos de 9 a 13 h")
        self.assertEqual(pedido.distri_vendedor_web_id, self.vendedor)
        # the customer's own order page shows it back
        page = self.url_open("/pedido/%s" % pedido.access_token).text
        self.assertIn("Recibimos de 9 a 13 h", page)
        # assigning the customer makes it their default for next time
        cliente = self.env["res.partner"].create({"name": "Cliente V98"})
        pedido.with_context(distri_asignar_cliente=True).write({"partner_id": cliente.id})
        self.assertEqual(cliente.distri_vendedor_web_id, self.vendedor)
        # an inactive or unknown seller is ignored
        post_pedido(self, self._data(vendedor_web_id=str(self.inactivo.id), telefono="2995550099"))
        self.assertFalse(self._order().distri_vendedor_web_id)

    def test_logged_customer_gets_the_seller_preselected(self):
        cliente = self.env["res.partner"].create({"name": "Cliente Logueado V98",
                                                  "distri_vendedor_web_id": self.vendedor.id})
        new_test_user(self.env, login="cliente_v98", groups="base.group_portal", partner_id=cliente.id)
        self.authenticate("cliente_v98", "cliente_v98")
        page = self.url_open("/pedido").text
        self.assertRegex(page, r'<option value="%s" selected="selected">Vendedora V98' % self.vendedor.id)

    def test_link_sellers_to_users_by_name(self):
        usuario = new_test_user(self.env, login="vendedora_v98", name="Vendedora V98",
                                groups="base.group_user")
        self.env["distri.vendedor.web"]._distri_vincular_usuarios()
        self.assertEqual(self.vendedor.user_id, usuario)

    def test_gear_on_public_pages_without_logout(self):
        for url in ("/", "/pedido", "/web/login"):
            page = self.url_open(url)
            self.assertEqual(page.status_code, 200, url)
            self.assertIn('id="dn-gear"', page.text, url)
            self.assertIn("Tamaño de letra", page.text, url)
            # 19.0.1.129.47: visitors get no «Instalar la app»
            self.assertNotIn("Instalar la app", page.text, url)
            self.assertNotIn("dn-aj__salir", page.text, "%s: no «Salir» for visitors" % url)

    def test_gear_on_staff_and_customer_pages(self):
        company = self.env.company
        cuentas = {
            "pagina_admin": ("base.group_user,distri_pedido.grupo_administracion,distri_pedido.grupo_cajero",
                             ["/tablero", "/despacho", "/cuentas", "/cobro", "/caja", "/armado",
                              "/armado/faltantes", "/mi-clave"], "Administración · Cajero"),
            "pagina_reparto": ("base.group_user,distri_pedido.grupo_reparto", ["/reparto", "/cobro"], "Reparto"),
            "pagina_armador": ("base.group_user,distri_pedido.grupo_armador", ["/armado"], "Armador"),
        }
        for login, (groups, urls, rol) in cuentas.items():
            new_test_user(self.env, login=login, name=login, groups=groups, company_id=company.id,
                          company_ids=[Command.set(company.ids)])
            self.authenticate(login, login)
            for url in urls:
                page = self.url_open(url)
                self.assertEqual(page.status_code, 200, "%s %s" % (login, url))
                self.assertIn('id="dn-gear"', page.text, "%s %s" % (login, url))
                self.assertIn('href="/web/session/logout"', page.text, "%s %s" % (login, url))
                self.assertIn(rol, page.text, "%s %s" % (login, url))
        # a customer: order page, account and password
        cliente = self.env["res.partner"].create({"name": "Cliente Engranaje V98"})
        new_test_user(self.env, login="cliente_gear_v98", groups="base.group_portal", partner_id=cliente.id)
        self.authenticate("cliente_gear_v98", "cliente_gear_v98")
        for url in ("/pedido", "/mi-clave"):
            page = self.url_open(url)
            self.assertEqual(page.status_code, 200, url)
            self.assertIn('id="dn-gear"', page.text, url)
            self.assertIn('href="/web/session/logout"', page.text, url)
            self.assertIn("Cliente", page.text, url)
            self.assertIn('href="/mi-clave"', page.text, url)
        # the install manifests
        self.assertEqual(self.url_open("/dn/cliente.webmanifest").json()["start_url"], "/pedido")
