"""Public and hidden price lists on /pedido (management decision of 29/09/2026).

The public sees only the General list («mayorista general», never the word
«minorista»). The hidden list (Distribuidoras: «May» prices and cigarettes to
be priced) reaches the page and is accepted on submit only for Gerencia,
Administración and logged-in customers whose company is flagged
distri_lista_oculta. Sellers see only the General list, like any shop, and do
not handle distributors (rule of 30/09/2026, models/lista_oculta.py). Prices
are always computed on the server. Synthetic data only.
"""
import json
import re

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, JsonRpcException, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.main import (
    CATEGORIAS_A_PRESUPUESTAR, DISTRIBUIDORAS, LISTA_GENERAL, LISTA_OCULTA,
    MENSAJE_DISTRIBUIDORA,
)
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido

PRECIO_GENERAL = 5123.0
PRECIO_MAY = 3777.77          # shown as "3.778", sent as data-precio="3777.77"
PRECIO_CIGARRILLO = 8888.0
PRECIO_REVENDEDOR = 7654.0


@tagged("post_install", "-at_install", "distri_pedido_listas")
class TestPedidoListas(PedidoAbiertoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Pricelist = cls.env["product.pricelist"]
        currency = cls.env.company.currency_id.id
        cls.general = Pricelist.search([("name", "=", LISTA_GENERAL)], limit=1) or \
            Pricelist.create({"name": LISTA_GENERAL, "currency_id": currency})
        cls.oculta = Pricelist.search([("name", "=", LISTA_OCULTA)], limit=1) or \
            Pricelist.create({"name": LISTA_OCULTA, "currency_id": currency,
                              "distri_mayorista": True})
        Categ = cls.env["product.category"]
        cls.categ_cig = Categ.search([("name", "=", CATEGORIAS_A_PRESUPUESTAR[0])], limit=1) or \
            Categ.create({"name": CATEGORIAS_A_PRESUPUESTAR[0]})
        base = {"type": "consu", "sale_ok": True, "is_published": True,
                "taxes_id": [Command.clear()]}
        Tmpl = cls.env["product.template"]
        cls.remedio = Tmpl.create(dict(base, name="Remedio Pruebalistas x 10",
                                       list_price=PRECIO_GENERAL))
        cls.cigarrillo = Tmpl.create(dict(base, name="Cigarrillo Pruebalistas 20",
                                          list_price=PRECIO_CIGARRILLO,
                                          categ_id=cls.categ_cig.id))
        cls.env["product.pricelist.item"].create({
            "pricelist_id": cls.oculta.id, "applied_on": "1_product",
            "product_tmpl_id": cls.remedio.id, "compute_price": "fixed",
            "fixed_price": PRECIO_MAY,
        })
        # a reseller list built on the hidden one, with its cigarette price
        cls.revendedor = Pricelist.create({
            "name": "Revendedor Pruebalistas", "currency_id": currency, "distri_mayorista": True,
            "item_ids": [
                Command.create({"applied_on": "1_product", "product_tmpl_id": cls.cigarrillo.id,
                                "compute_price": "fixed", "fixed_price": PRECIO_REVENDEDOR}),
                Command.create({"applied_on": "3_global", "compute_price": "formula",
                                "base": "pricelist", "base_pricelist_id": cls.oculta.id}),
            ],
        })
        Partner = cls.env["res.partner"]
        # authorized distributor with the General list: sees the section
        cls.distribuidora = Partner.create({
            "name": "Distribuidora Pruebalistas", "company_type": "company",
            "distri_lista_oculta": True, "property_product_pricelist": cls.general.id})
        # authorized reseller with its own hidden list: sees its prices
        cls.revendedora = Partner.create({
            "name": "Revendedora Pruebalistas", "company_type": "company",
            "distri_lista_oculta": True, "property_product_pricelist": cls.revendedor.id})
        # a hidden list assigned but no authorization: sees the General list only
        cls.kiosco = Partner.create({
            "name": "Kiosco Pruebalistas", "company_type": "company",
            "property_product_pricelist": cls.revendedor.id})
        cls._portal("distri_listas", cls.distribuidora)
        cls._portal("revende_listas", cls.revendedora)
        cls._portal("kiosco_listas", cls.kiosco)
        cls.cartera = cls.env["res.partner.category"].create({"name": "Cartera Pruebalistas"})
        # a shop of the seller's portfolio, with the General list
        cls.almacen = Partner.create({
            "name": "Almacen Pruebalistas", "company_type": "company",
            "category_id": [Command.link(cls.cartera.id)],
            "property_product_pricelist": cls.general.id})
        cls.vendedor = new_test_user(cls.env, login="vendedor_listas", name="Vendedor Listas",
                                     groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.vendedor.etiqueta_cartera_id = cls.cartera
        # the distributor is in the portfolio too: even so the seller cannot
        # give it a hidden list or handle its orders
        cls.distribuidora.category_id = [Command.link(cls.cartera.id)]
        cls.administracion = new_test_user(
            cls.env, login="admin_listas", name="Administracion Listas",
            groups="base.group_user,distri_pedido.grupo_administracion")
        cls.gerencia = new_test_user(
            cls.env, login="gerencia_listas", name="Gerencia Listas",
            groups="base.group_user,distri_pedido.grupo_gerencia")

    @classmethod
    def _portal(cls, login, company):
        # the contact of the company logs in: the flag is read on the company
        contacto = cls.env["res.partner"].create({
            "name": "Contacto %s" % login, "parent_id": company.id, "type": "contact"})
        new_test_user(cls.env, login=login, groups="base.group_portal",
                      partner_id=contacto.id)

    # ------------------------------------------------------------ helpers

    def _pedido_html(self, url="/pedido"):
        res = self.url_open(url)
        self.assertEqual(res.status_code, 200)
        return res.text

    def _assert_publica(self, html):
        """The page of a visitor without authorization."""
        self.assertNotIn("inorista", html.lower())
        self.assertNotIn(DISTRIBUIDORAS["nombre"], html)
        self.assertNotIn(DISTRIBUIDORAS["detalle"], html)
        self.assertNotIn("Cigarrillos y tabaco a presupuestar", html)
        self.assertNotIn("data-may", html)
        self.assertNotIn('name="modo"', html)
        for precio in ("3777.77", "3.778", "7654", "7.654", "8888", "8.888"):
            self.assertNotIn(precio, html)
        # the General price is there, and the cigarette has no public price
        self.assertIn('data-id="%s" data-precio="%s"' % (self.remedio.id, PRECIO_GENERAL), html)
        # a zero price is not even rendered: QWeb drops the falsy attribute
        self.assertRegex(html, r'data-id="%s"(\s+data-precio="0(\.0)?")?\s+data-sinprecio="1"'
                         % self.cigarrillo.id)

    def _enviar(self, **extra):
        data = {
            "renglones": json.dumps([
                {"id": self.remedio.id, "cantidad": 20, "may": 1},
                {"id": self.cigarrillo.id, "cantidad": 1, "may": 1},
            ]),
            "nombre": "Comprador Pruebalistas %s" % len(extra),
            "telefono": "2995550101", "domicilio": "Calle Falsa 123", "entrega": "domicilio",
        }
        data.update(extra)
        res = post_pedido(self, data)
        self.assertEqual(res.status_code, 200)
        return self.env["sale.order"].search(
            [("order_line.product_id", "=", self.remedio.product_variant_id.id)],
            order="id desc", limit=1)

    def _csrf(self):
        return re.search(r'name="csrf_token" value="([^"]+)"',
                         self._pedido_html()).group(1)

    def _pedido_de_distribuidora(self):
        pedido = self.env["sale.order"].create({
            "partner_id": self.distribuidora.id, "pricelist_id": self.oculta.id,
            "order_line": [Command.create({"product_id": self.remedio.product_variant_id.id,
                                           "product_uom_qty": 1})],
        })
        pedido._portal_ensure_token()
        return pedido

    def _precio(self, pedido, plantilla):
        return pedido.order_line.filtered(
            lambda l: l.product_id.product_tmpl_id == plantilla).price_unit

    # -------------------------------------------------------------- tests

    def test_anonymous_page_shows_only_the_general_list(self):
        html = self._pedido_html()
        self._assert_publica(html)
        self.assertIn("Precios de la lista mayorista general", html)

    def test_anonymous_home_page_does_not_announce_the_hidden_list(self):
        html = self._pedido_html("/")
        self.assertNotIn("inorista", html.lower())
        self.assertNotIn("sección <b>Mayorista</b>", html)
        self.assertNotIn(DISTRIBUIDORAS["nombre"], html)

    def test_crafted_hidden_mode_post_gets_general_prices(self):
        pedido = self._enviar(modo="mayorista")
        self.assertEqual(self._precio(pedido, self.remedio), PRECIO_GENERAL)
        # cigarettes have no public price: in at zero, priced by the seller
        self.assertEqual(self._precio(pedido, self.cigarrillo), 0.0)
        self.assertEqual(pedido.pricelist_id, self.general)
        self.assertEqual(pedido.partner_id.distri_generic_kind, "retail")
        self.assertFalse(any(r["may"] for r in json.loads(pedido.distri_original)))
        self.assertNotIn(LISTA_OCULTA, pedido.note)
        # editing it with the link: still no hidden section, quantities kept
        pedido._portal_ensure_token()
        html = self._pedido_html("/pedido/%s/modificar" % pedido.access_token)
        self._assert_publica(html)

    def test_authorized_customer_sees_the_hidden_section(self):
        self.authenticate("distri_listas", "distri_listas")
        html = self._pedido_html()
        self.assertIn(DISTRIBUIDORAS["nombre"], html)
        self.assertIn("Cigarrillos y tabaco a presupuestar", html)
        self.assertIn('data-precio="%s"' % PRECIO_MAY, html)
        self.assertIn("data-may", html)
        self.assertNotIn("inorista", html.lower())
        pedido = self._enviar()
        self.assertAlmostEqual(self._precio(pedido, self.remedio), PRECIO_MAY, places=2)
        self.assertEqual(self._precio(pedido, self.cigarrillo), 0.0)
        self.assertEqual(pedido.pricelist_id, self.oculta)
        self.assertEqual(pedido.partner_id.distri_generic_kind, "wholesale")

    def test_authorized_reseller_sees_its_own_list(self):
        self.authenticate("revende_listas", "revende_listas")
        html = self._pedido_html()
        self.assertIn("(lista Revendedor Pruebalistas)", html)
        self.assertIn('data-id="%s" data-precio="%s"' % (self.cigarrillo.id, PRECIO_REVENDEDOR),
                      html)
        self.assertIn('data-id="%s" data-precio="%s"' % (self.remedio.id, PRECIO_MAY), html)
        # its prices are already in the catalog: no separate section
        self.assertNotIn("data-may", html)

    def test_unauthorized_logged_in_customer_sees_the_general_list(self):
        self.authenticate("kiosco_listas", "kiosco_listas")
        html = self._pedido_html()
        self._assert_publica(html)
        self.assertIn("(lista %s)" % LISTA_GENERAL, html)
        pedido = self._enviar(modo="mayorista")
        self.assertEqual(self._precio(pedido, self.remedio), PRECIO_GENERAL)
        self.assertEqual(self._precio(pedido, self.cigarrillo), 0.0)
        self.assertEqual(pedido.pricelist_id, self.general)

    def test_gerencia_and_administracion_see_the_hidden_section(self):
        for login in ("admin_listas", "gerencia_listas"):
            self.authenticate(login, login)
            html = self._pedido_html()
            self.assertIn(DISTRIBUIDORAS["nombre"], html, login)
            self.assertIn('data-precio="%s"' % PRECIO_MAY, html, login)

    def test_seller_sees_only_the_general_list(self):
        self.authenticate("vendedor_listas", "vendedor_listas")
        self._assert_publica(self._pedido_html())

    def test_crafted_hidden_post_by_a_seller_gets_general_prices(self):
        self.authenticate("vendedor_listas", "vendedor_listas")
        pedido = self._enviar(csrf_token=self._csrf(), cliente_id=str(self.almacen.id))
        self.assertEqual(pedido.partner_id, self.almacen)
        self.assertEqual(pedido.user_id, self.vendedor)
        self.assertEqual(pedido.pricelist_id, self.general)
        self.assertEqual(self._precio(pedido, self.remedio), PRECIO_GENERAL)
        self.assertFalse(any(r["may"] for r in json.loads(pedido.distri_original)))
        # the seller's own order screens: General prices, no hidden section
        html = self._pedido_html("/pedido/%s/modificar" % pedido.access_token)
        self._assert_publica(html)
        html = self._pedido_html("/pedido/%s" % pedido.access_token)
        for precio in ("3.778", "7.654"):
            self.assertNotIn(precio, html)

    def test_seller_cannot_take_or_open_a_distributor_order(self):
        self.authenticate("vendedor_listas", "vendedor_listas")
        csrf = self._csrf()
        # not offered in the customer search, refused if posted anyway
        nombres = [f["nombre"] for f in
                   self.url_open("/pedido/clientes?q=Pruebalistas").json()]
        self.assertIn(self.almacen.name, nombres)
        for socio in (self.distribuidora, self.revendedora, self.kiosco):
            self.assertNotIn(socio.name, nombres)
        antes = self.env["sale.order"].search_count([])
        res = post_pedido(self, {
            "renglones": json.dumps([{"id": self.remedio.id, "cantidad": 1}]),
            "csrf_token": csrf, "cliente_id": str(self.revendedora.id)})
        self.assertEqual(res.status_code, 403)
        self.assertIn(MENSAJE_DISTRIBUIDORA, res.text)
        self.assertEqual(self.env["sale.order"].search_count([]), antes)
        # a distributor's order: not shown, not edited, not repriced
        pedido = self._pedido_de_distribuidora()
        total = pedido.amount_total
        for url in ("/pedido/%s", "/pedido/%s/modificar", "/pedido/%s/cuenta"):
            res = self.url_open(url % pedido.access_token)
            self.assertEqual(res.status_code, 403, url)
            self.assertIn(MENSAJE_DISTRIBUIDORA, res.text)
            self.assertNotIn("3.778", res.text)
        res = post_pedido(self, {
            "renglones": json.dumps([{"id": self.remedio.id, "cantidad": 9}]),
            "csrf_token": csrf, "token": pedido.access_token})
        self.assertEqual(res.status_code, 403)
        pedido.invalidate_recordset()
        self.assertEqual(pedido.amount_total, total)
        self.assertEqual(pedido.order_line.product_uom_qty, 1)
        # the distributor itself still opens it
        self.authenticate("distri_listas", "distri_listas")
        self.assertEqual(self.url_open("/pedido/%s" % pedido.access_token).status_code, 200)

    def test_seller_cannot_read_hidden_lists_or_distributor_orders(self):
        pedido = self._pedido_de_distribuidora()
        Pricelist = self.env["product.pricelist"].with_user(self.vendedor)
        listas = Pricelist.search([])
        self.assertIn(self.general, listas)
        self.assertNotIn(self.oculta, listas)
        self.assertNotIn(self.revendedor, listas)
        items = self.env["product.pricelist.item"].with_user(self.vendedor).search([])
        self.assertFalse(items & (self.oculta.item_ids | self.revendedor.item_ids))
        with self.assertRaises(AccessError):
            self.oculta.item_ids.with_user(self.vendedor).read(["fixed_price"])
        Order = self.env["sale.order"].with_user(self.vendedor)
        self.assertNotIn(pedido, Order.search([]))
        with self.assertRaises(AccessError):
            pedido.with_user(self.vendedor).read(["amount_total"])
        with self.assertRaises(AccessError):
            pedido.order_line.with_user(self.vendedor).read(["price_unit"])
        with self.assertRaisesRegex(UserError, "lo gestiona Administración"):
            pedido.with_user(self.vendedor).write({"note": "cambio"})
        # Gerencia and Administración see everything
        for usuario in (self.administracion, self.gerencia):
            self.assertIn(self.oculta, self.env["product.pricelist"].with_user(usuario).search([]))
            self.assertIn(pedido, self.env["sale.order"].with_user(usuario).search([]))
            pedido.with_user(usuario).read(["amount_total"])

    def test_seller_cannot_assign_a_hidden_list(self):
        # a plain seller cannot write contacts at all; one that was also given
        # «Contactos: creación» still cannot assign a hidden list
        with self.assertRaises(AccessError):
            self.almacen.with_user(self.vendedor).write(
                {"property_product_pricelist": self.oculta.id})
        con_contactos = new_test_user(
            self.env, login="vendedor_contactos_listas",
            groups="base.group_user,base.group_partner_manager,distri_pedido.grupo_vendedor")
        con_contactos.etiqueta_cartera_id = self.cartera
        socio = self.almacen.with_user(con_contactos)
        for lista in (self.oculta, self.revendedor):
            with self.assertRaisesRegex(UserError, "Gerencia y Administración"):
                socio.write({"property_product_pricelist": lista.id})
        socio.write({"property_product_pricelist": self.general.id})
        self.assertEqual(self.almacen.property_product_pricelist, self.general)
        Order = self.env["sale.order"].with_user(self.vendedor)
        with self.assertRaisesRegex(UserError, "lo gestiona Administración"):
            Order.create({"partner_id": self.almacen.id, "pricelist_id": self.oculta.id})
        with self.assertRaisesRegex(UserError, "lo gestiona Administración"):
            Order.create({"partner_id": self.distribuidora.id})
        propio = Order.create({"partner_id": self.almacen.id})
        self.assertEqual(propio.pricelist_id, self.general)
        with self.assertRaisesRegex(UserError, "lo gestiona Administración"):
            propio.write({"pricelist_id": self.revendedor.id})
        # Gerencia and Administración assign it
        self.almacen.with_user(self.administracion).write(
            {"property_product_pricelist": self.revendedor.id})
        self.assertEqual(self.almacen.property_product_pricelist, self.revendedor)
        propio.with_user(self.gerencia).write({"pricelist_id": self.oculta.id})
        self.assertEqual(propio.pricelist_id, self.oculta)

    # ------------------------------------ customers and the data API

    def _usuario(self, login):
        return self.env["res.users"].search([("login", "=", login)])

    def _visibles(self, usuario):
        listas = self.env["product.pricelist"].with_user(usuario).search([])
        items = self.env["product.pricelist.item"].with_user(usuario).search_read(
            [], ["pricelist_id", "fixed_price"])
        return listas, {i["pricelist_id"][0] for i in items}

    def test_customers_read_only_public_and_own_authorized_lists(self):
        oculta_item = self.oculta.item_ids[:1]
        revendedor_item = self.revendedor.item_ids[:1]
        casos = (
            # (user, own hidden list it may read)
            (self._usuario("kiosco_listas"), None),          # hidden list, not authorized
            (self._usuario("distri_listas"), None),          # authorized, General list
            (self.env.ref("base.public_user"), None),        # visitor
            (self._usuario("revende_listas"), self.revendedor),
        )
        for usuario, propia in casos:
            listas, con_items = self._visibles(usuario)
            self.assertIn(self.general, listas, usuario.login)
            self.assertNotIn(self.oculta, listas, usuario.login)
            self.assertNotIn(self.oculta.id, con_items, usuario.login)
            with self.assertRaises(AccessError):
                oculta_item.with_user(usuario).read(["fixed_price"])
            if propia:
                self.assertIn(propia, listas)
                self.assertIn(propia.id, con_items)
                revendedor_item.with_user(usuario).read(["fixed_price"])
            else:
                self.assertNotIn(self.revendedor, listas, usuario.login)
                self.assertNotIn(self.revendedor.id, con_items, usuario.login)
                with self.assertRaises(AccessError):
                    revendedor_item.with_user(usuario).read(["fixed_price"])
            # a price asked for with a hidden list in the context
            variante = self.remedio.product_variant_id.with_user(usuario).with_context(
                pricelist=self.oculta.id)
            try:
                precio = variante.get_contextual_price()
            except AccessError:
                precio = None
            self.assertNotAlmostEqual(precio or 0.0, PRECIO_MAY, places=2)

    def test_customer_authorization_is_applied_at_once(self):
        kiosco = self._usuario("kiosco_listas")
        self.assertNotIn(self.revendedor, self._visibles(kiosco)[0])
        # Administración flags the company: its own hidden list becomes readable
        self.kiosco.with_user(self.administracion).write({"distri_lista_oculta": True})
        self.assertIn(self.revendedor, self._visibles(kiosco)[0])
        self.kiosco.with_user(self.administracion).write(
            {"property_product_pricelist": self.general.id})
        self.assertNotIn(self.revendedor, self._visibles(kiosco)[0])

    def test_customer_cannot_read_other_customers_orders(self):
        pedido = self._pedido_de_distribuidora()
        kiosco = self._usuario("kiosco_listas")
        self.assertNotIn(pedido, self.env["sale.order"].with_user(kiosco).search([]))
        self.assertFalse(self.env["sale.order.line"].with_user(kiosco).search(
            [("order_id", "=", pedido.id)]))
        with self.assertRaises(AccessError):
            pedido.order_line.with_user(kiosco).read(["price_unit"])
        # the distributor reads its own order
        pedido.with_user(self._usuario("distri_listas")).read(["amount_total"])

    def test_json_rpc_as_customer_gets_no_hidden_prices(self):
        self.authenticate("kiosco_listas", "kiosco_listas")

        def call_kw(model, method, args, kwargs=None):
            return self.make_jsonrpc_request("/web/dataset/call_kw", {
                "model": model, "method": method, "args": args, "kwargs": kwargs or {}})

        items = call_kw("product.pricelist.item", "search_read", [[]],
                        {"fields": ["pricelist_id", "fixed_price"]})
        ocultas = {self.oculta.id, self.revendedor.id}
        self.assertFalse([i for i in items if i["pricelist_id"][0] in ocultas])
        for precio in (PRECIO_MAY, PRECIO_REVENDEDOR):
            self.assertNotIn(precio, [i["fixed_price"] for i in items])
        nombres = [p["name"] for p in call_kw("product.pricelist", "search_read", [[]],
                                              {"fields": ["name"]})]
        self.assertIn(LISTA_GENERAL, nombres)
        self.assertNotIn(LISTA_OCULTA, nombres)
        self.assertNotIn(self.revendedor.name, nombres)
        with self.assertRaises(JsonRpcException):
            call_kw("product.pricelist.item", "read",
                    [self.oculta.item_ids.ids, ["fixed_price"]])
        try:
            precio = call_kw("product.product", "get_contextual_price",
                             [[self.remedio.product_variant_id.id]],
                             {"context": {"pricelist": self.oculta.id}})
        except JsonRpcException:
            precio = None
        self.assertNotAlmostEqual(precio or 0.0, PRECIO_MAY, places=2)
        # the page keeps working for this customer
        self._assert_publica(self._pedido_html())

    def test_only_administracion_can_flag_a_customer(self):
        with self.assertRaises(AccessError):
            self.kiosco.with_user(self.vendedor).write({"distri_lista_oculta": True})
        self.kiosco.with_user(self.administracion).write({"distri_lista_oculta": True})
        self.assertTrue(self.kiosco.distri_lista_oculta)
