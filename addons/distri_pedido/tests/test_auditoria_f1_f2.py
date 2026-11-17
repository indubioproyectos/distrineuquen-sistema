"""Audit 2026-11-17, findings F1, F2 and F9 (19.0.1.96.0).

F1: /pedido/enviar requires the page's own fields on the server, refuses
    fractions and absurd quantities/totals from the public page, and an
    anonymous order is held out of /armado until administración assigns the
    customer and releases it. Staff and logged-in customers: unchanged.
F2: armador and reparto read no customers and no hidden price lists through
    the raw data API; their screens keep working (they read with sudo).
F9: the statement wizard follows the seller's portfolio.
Synthetic data only.
"""
import json

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tests.common import JsonRpcException

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.controllers.main import PARAM_MAX_TOTAL, PARAM_MAX_UNIDADES
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

NOMBRE = "Kiosco Auditoria F1"


@tagged("post_install", "-at_install", "distri_auditoria")
class TestPedidoAnonimo(PedidoAbiertoMixin, HttpCase):
    """F1 through the real form."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # one unit reaches the minimum purchase (COMPRA_MINIMA)
        cls.plantilla = cls.env["product.template"].create({
            "name": "Producto Auditoria F1", "list_price": 70000.0, "is_published": True,
            "sale_ok": True, "type": "consu", "taxes_id": [Command.clear()]})
        cls.armador = new_test_user(cls.env, login="armador_f1", name="Armador F1",
                                    groups="base.group_user,distri_pedido.grupo_armador")
        cls.admin = new_test_user(cls.env, login="admin_f1", name="Administracion F1",
                                  groups="base.group_user,distri_pedido.grupo_administracion")
        cls.vendedor = new_test_user(cls.env, login="vendedor_f1", name="Vendedor F1",
                                     groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.cliente_portal = cls.env["res.partner"].create({
            "name": "Almacen Portal F1", "company_type": "company"})
        cls.portal = new_test_user(cls.env, login="portal_f1", name="Portal F1",
                                   groups="base.group_portal",
                                   partner_id=cls.cliente_portal.id)
        cls.cliente = cls.env["res.partner"].create({
            "name": "Despensa Asignada F1", "company_type": "company"})

    def _data(self, cantidad=1, **extra):
        data = {"renglones": json.dumps([{"id": self.plantilla.id, "cantidad": cantidad}]),
                "nombre": NOMBRE, "telefono": "2995550111", "domicilio": "Calle F1 123",
                "entrega": "domicilio"}
        data.update(extra)
        return data

    def _orders(self, nombre=NOMBRE):
        return self.env["sale.order"].sudo().search([("distri_nombre", "=", nombre)],
                                                    order="id")

    def _armado_html(self):
        self.authenticate("armador_f1", "armador_f1")
        res = self.url_open("/armado")
        self.assertEqual(res.status_code, 200)
        return res.text

    # ------------------------------------------------------------ fields

    def test_empty_contact_fields_are_refused(self):
        """The audit's S00177: no name, phone nor address."""
        antes = self.env["sale.order"].sudo().search_count([])
        res = post_pedido(self, self._data(nombre="", telefono="", domicilio=""))
        self.assertEqual(res.status_code, 400)
        self.assertIn("Faltan tus datos", res.text)
        self.assertIn("el nombre del comercio, un teléfono de contacto y el domicilio de entrega",
                      res.text)
        # a one-line error without a form: sin_historial.js shows it in place
        self.assertNotIn("<form", res.text)
        self.assertEqual(self.env["sale.order"].sudo().search_count([]), antes)
        for falta, data in (("el nombre del comercio", {"nombre": "   "}),
                            ("un teléfono de contacto", {"telefono": " "}),
                            ("el domicilio de entrega", {"domicilio": ""})):
            res = post_pedido(self, self._data(**data))
            self.assertEqual(res.status_code, 400, falta)
            self.assertIn("poné %s." % falta, res.text)
        self.assertFalse(self._orders())

    def test_pickup_and_parcel_need_no_address(self):
        res = post_pedido(self, self._data(domicilio="", entrega="retira"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self._orders()), 1)

    # ------------------------------------------------------------ the hold

    def test_anonymous_order_waits_out_of_packing_until_released(self):
        res = post_pedido(self, self._data())
        self.assertEqual(res.status_code, 200)
        order = self._orders()
        self.assertEqual(len(order), 1)
        self.assertTrue(order._distri_sin_cliente())
        self.assertTrue(order.distri_retenido)
        self.assertTrue(order.distri_retenido_sin_cliente)
        self.assertEqual(order.distri_semaforo, "rojo")
        self.assertFalse(order._distri_armable())
        # nor in the list, nor its counter, nor opened by id
        html = self._armado_html()
        self.assertNotIn(NOMBRE, html)
        self.assertNotIn(order.name, html)
        self.assertEqual(self.url_open("/armado/pedido/%s" % order.id).status_code, 404)
        # it is in «Pedidos para revisar»
        self.assertIn(order, self.env["sale.order"].search([("distri_revisar_en_lista", "=", True)]))
        # releasing without a customer is refused
        with self.assertRaises(UserError):
            order.with_user(self.admin).action_distri_liberar()
        # the customer is assigned: still held until released
        order.with_user(self.admin).with_context(distri_asignar_cliente=True).write(
            {"partner_id": self.cliente.id})
        self.assertFalse(order._distri_armable())
        self.assertNotIn("Despensa Asignada F1", self._armado_html())
        order.with_user(self.admin).action_distri_liberar()
        self.assertFalse(order.distri_retenido)
        self.assertFalse(order.distri_retenido_sin_cliente)
        self.assertTrue(order._distri_armable())
        self.assertIn("Despensa Asignada F1", self._armado_html())

    def test_assign_and_approve_releases_the_hold(self):
        post_pedido(self, self._data())
        order = self._orders()
        self.assertTrue(order.distri_retenido_sin_cliente)
        wizard = self.env["distri.asignar.cliente"].with_user(self.admin).create({
            "order_id": order.id, "modo": "existente", "partner_id": self.cliente.id})
        wizard.action_asignar_y_aprobar()
        self.assertFalse(order.distri_retenido)
        self.assertEqual(order.state, "sale")
        self.assertTrue(order._distri_armable())

    def test_logged_in_customer_and_staff_orders_are_not_held(self):
        self.authenticate("portal_f1", "portal_f1")
        res = post_pedido(self, self._data(nombre="Portal F1 pedido"))
        self.assertEqual(res.status_code, 200)
        order = self._orders("Portal F1 pedido")
        self.assertFalse(order.distri_retenido)
        self.assertTrue(order._distri_armable())
        self.authenticate("vendedor_f1", "vendedor_f1")
        res = post_pedido(self, self._data(nombre="Vendedor F1 pedido"))
        self.assertEqual(res.status_code, 200)
        order = self._orders("Vendedor F1 pedido")
        self.assertEqual(order.distri_wa_state, "staff")
        self.assertFalse(order.distri_retenido)
        self.assertTrue(order._distri_armable())
        self.assertIn("Vendedor F1 pedido", self._armado_html())

    # ------------------------------------------------------------ quantities

    def test_quantity_and_total_caps(self):
        for cantidad in (0, 0.5, 1.6, -2):
            res = post_pedido(self, self._data(cantidad=cantidad))
            self.assertEqual(res.status_code, 400, cantidad)
        res = post_pedido(self, self._data(cantidad=1001))
        self.assertEqual(res.status_code, 400)
        self.assertIn("máximo 1000", res.text)
        self.assertFalse(self._orders())
        params = self.env["ir.config_parameter"].sudo()
        params.set_param(PARAM_MAX_UNIDADES, "5")
        res = post_pedido(self, self._data(cantidad=6))
        self.assertIn("máximo 5", res.text)
        params.set_param(PARAM_MAX_UNIDADES, "1000")
        # the total: $ 140,000 above a cap of $ 100,000
        params.set_param(PARAM_MAX_TOTAL, "100000")
        res = post_pedido(self, self._data(cantidad=2))
        self.assertEqual(res.status_code, 400)
        self.assertIn("por la página se pueden enviar hasta", res.text)
        self.assertFalse(self._orders())
        # staff are not capped
        self.authenticate("vendedor_f1", "vendedor_f1")
        res = post_pedido(self, self._data(cantidad=2))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._orders().order_line.product_uom_qty, 2)
        # "0" turns the cap off
        self.authenticate(None, None)
        params.set_param(PARAM_MAX_TOTAL, "0")
        res = post_pedido(self, self._data(cantidad=3, nombre="Sin tope F1"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self._orders("Sin tope F1")), 1)


@tagged("post_install", "-at_install", "distri_auditoria")
class TestPermisosArmadorReparto(RepartoMixin, AccountTestInvoicingCommon):
    """F2 and F9 through the ORM, as the audit read them."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.armador = new_test_user(cls.env, login="armador_f2", name="Armador F2",
                                    groups="base.group_user,distri_pedido.grupo_armador",
                                    **company)
        cls.cartera = cls.env["res.partner.category"].create({"name": "Cartera F2"})
        cls.mixto = new_test_user(
            cls.env, login="reparto_vendedor_f2", name="Reparto y Vendedor F2",
            groups="base.group_user,distri_pedido.grupo_reparto,distri_pedido.grupo_vendedor",
            **company)
        cls.mixto.etiqueta_cartera_id = cls.cartera
        cls.admin = new_test_user(cls.env, login="admin_f2", name="Administracion F2",
                                  groups="base.group_user,distri_pedido.grupo_administracion",
                                  **company)
        Partner = cls.env["res.partner"]
        # the audit read the two customers marked uncollectible
        cls.ajeno = Partner.create({
            "name": "Cliente Ajeno F2", "company_type": "company", "vat": "20123456786",
            "distri_uncollectible": True})
        cls.propio = Partner.create({"name": "Cliente Cartera F2", "company_type": "company",
                                     "category_id": [Command.link(cls.cartera.id)]})
        Pricelist = cls.env["product.pricelist"]
        currency = cls.env.company.currency_id.id
        cls.general = Pricelist.create({"name": "General F2", "currency_id": currency})
        cls.oculta = Pricelist.create({"name": "Revendedor F2", "currency_id": currency,
                                       "distri_mayorista": True})
        cls.item_oculto = cls.env["product.pricelist.item"].create({
            "pricelist_id": cls.oculta.id, "compute_price": "fixed", "fixed_price": 1.0,
            "applied_on": "1_product", "product_tmpl_id": cls.product_a.product_tmpl_id.id})

    def _api_reads(self, user):
        Partner = self.env["res.partner"].with_user(user)
        return {
            "partners": Partner.search([]),
            "customers": Partner.search([("customer_rank", ">=", 0), ("is_company", "=", True)]),
            "uncollectible": Partner.search_count([("distri_uncollectible", "=", True)]),
            "pricelists": self.env["product.pricelist"].with_user(user).search([]),
            "items": self.env["product.pricelist.item"].with_user(user).search([]),
        }

    def test_packer_and_driver_read_no_customers_nor_hidden_lists(self):
        for user in (self.armador, self.driver):
            reads = self._api_reads(user)
            self.assertNotIn(self.ajeno, reads["partners"], user.login)
            self.assertNotIn(self.propio, reads["partners"], user.login)
            self.assertEqual(reads["uncollectible"], 0, user.login)
            # only their own contact and the company's
            self.assertIn(user.partner_id, reads["partners"])
            self.assertIn(self.env.company.partner_id, reads["partners"])
            self.assertNotIn(self.other_driver.partner_id, reads["partners"])
            self.assertFalse(reads["partners"] - user.partner_id
                             - self.env["res.company"].sudo().search([]).partner_id, user.login)
            with self.assertRaises(AccessError):
                self.ajeno.with_user(user).read(["name", "vat"])
            self.assertNotIn(self.oculta, reads["pricelists"])
            self.assertIn(self.general, reads["pricelists"])
            self.assertNotIn(self.item_oculto, reads["items"])
            with self.assertRaises(AccessError):
                self.oculta.with_user(user).read(["name"])
            with self.assertRaises(AccessError):
                self.item_oculto.with_user(user).read(["fixed_price"])

    def test_multi_group_users_combine(self):
        """A driver who is also a seller keeps his portfolio (group rules
        are OR'ed) and still no hidden list; administración sees all."""
        reads = self._api_reads(self.mixto)
        self.assertIn(self.propio, reads["partners"])
        self.assertNotIn(self.ajeno, reads["partners"])
        # the driver rule adds no colleague to what the seller rule shows
        self.assertNotIn(self.driver.partner_id, reads["partners"])
        self.assertNotIn(self.oculta, reads["pricelists"])
        reads = self._api_reads(self.admin)
        self.assertIn(self.propio, reads["partners"])
        self.assertIn(self.ajeno, reads["partners"])
        self.assertIn(self.oculta, reads["pricelists"])
        self.assertIn(self.item_oculto, reads["items"])

    def test_driver_screens_still_read_the_customer(self):
        """/reparto reads with sudo: the stop still carries the customer."""
        order = self._order(self.ajeno, self.product_a)
        route = self._route(order)
        payload = route.with_user(self.driver).sudo()._app_payload()
        self.assertIn("Cliente Ajeno F2", json.dumps(payload, default=str))

    def test_statement_wizard_follows_the_portfolio(self):
        """F9: the seller reads the statement of his customers only."""
        Wizard = self.env["distri.estado.cuenta"].with_user(self.mixto)
        wizard = Wizard.create({"partner_id": self.propio.id})
        self.assertIsInstance(wizard.balance, float)
        with self.assertRaises(AccessError):
            Wizard.create({"partner_id": self.ajeno.id})
        # administración: any customer
        wizard = self.env["distri.estado.cuenta"].with_user(self.admin).create(
            {"partner_id": self.ajeno.id})
        self.assertIsInstance(wizard.balance, float)


@tagged("post_install", "-at_install", "distri_auditoria")
class TestPermisosApiHttp(HttpCase):
    """F2 through JSON-RPC, the way the audit read it (t14_permisos.py)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.armador = new_test_user(cls.env, login="armador_f2_http", name="Armador F2 HTTP",
                                    groups="base.group_user,distri_pedido.grupo_armador")
        cls.repartidor = new_test_user(cls.env, login="reparto_f2_http",
                                       name="Reparto F2 HTTP",
                                       groups="base.group_user,distri_pedido.grupo_reparto")
        cls.cliente = cls.env["res.partner"].create({
            "name": "Cliente Http F2", "company_type": "company", "customer_rank": 1})
        cls.oculta = cls.env["product.pricelist"].create({
            "name": "Distribuidoras Http F2", "distri_mayorista": True,
            "currency_id": cls.env.company.currency_id.id})

    def _call(self, model, method, args, kwargs=None):
        return self.make_jsonrpc_request("/web/dataset/call_kw", {
            "model": model, "method": method, "args": args, "kwargs": kwargs or {}})

    def test_raw_api_reads(self):
        for login in ("armador_f2_http", "reparto_f2_http"):
            self.authenticate(login, login)
            partners = self._call("res.partner", "search_read",
                                  [[["customer_rank", ">", 0]]], {"fields": ["name", "vat"]})
            self.assertEqual(partners, [], login)
            ids = [p["id"] for p in self._call("res.partner", "search_read", [[]],
                                                {"fields": ["name"]})]
            self.assertNotIn(self.cliente.id, ids, login)
            with self.assertRaises(JsonRpcException):
                self._call("res.partner", "read", [[self.cliente.id], ["name"]])
            names = [p["name"] for p in self._call("product.pricelist", "search_read", [[]],
                                                    {"fields": ["name"]})]
            self.assertNotIn("Distribuidoras Http F2", names, login)
            self.assertFalse(self._call("product.pricelist.item", "search_count",
                                        [[["pricelist_id", "=", self.oculta.id]]]))
            # their own screen still opens
            screen = "/armado" if login.startswith("armador") else "/reparto"
            self.assertEqual(self.url_open(screen).status_code, 200, screen)
