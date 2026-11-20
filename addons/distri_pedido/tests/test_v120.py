"""19.0.1.120.0: staff land on Inicio; wholesale customers on the routes.

Feature 1 (owner, day 1): every packer and driver lands on Inicio
(/tablero) with a tile for each screen he may open, and every staff screen
has a way back to it (the «Inicio» button next to the floating gear, the
first link of the «Ajustes» sheet, kit.staff_nav). A seller locked to
/cuentas who neither packs nor delivers keeps landing on /cuentas.

Feature 2 (owner): administración and gerencia see the wholesale customers
(«Es mayorista», hidden lists) and their balance in /cuentas and plan their
orders on /rutas; the driver gets the stop of a wholesale order on his route
(name, address, phone, what to collect) and delivers and collects it like any
other, but «Ver cuenta» of that customer stays with administración.
"""
from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

LOCK = "distri_pedido.vendedor_solo_cuentas"


def _user(env, login, groups):
    company = env.company
    return new_test_user(env, login=login, name=login.replace("_", " ").title(),
                         groups="base.group_user," + groups, company_id=company.id,
                         company_ids=[Command.set(company.ids)])


@tagged("post_install", "-at_install", "distri_v120", "distri_v120_inicio")
class TestV120Inicio(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.env["ir.config_parameter"].sudo().set_param(LOCK, "1")
        cls.packer = _user(env, "facu_v120", "distri_pedido.grupo_armador")
        cls.driver = _user(env, "chofer_v120", "distri_pedido.grupo_reparto")
        cls.simon = _user(env, "simon_v120",
                          "distri_pedido.grupo_reparto,distri_pedido.grupo_armador")
        cls.italo = _user(env, "italo_v120", "distri_pedido.grupo_reparto,"
                          "distri_pedido.grupo_armador,distri_pedido.grupo_vendedor")
        cls.seller = _user(env, "vendedor_v120", "distri_pedido.grupo_vendedor")
        cls.admin = _user(env, "denis_v120", "distri_pedido.grupo_administracion")

    def _login(self, login):
        self.url_open("/web/session/logout")
        page = self.url_open("/web/login")
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
        res = self.url_open("/web/login", data={"login": login, "password": login,
                                                "csrf_token": csrf}, allow_redirects=False)
        self.assertEqual(res.status_code, 303, login)
        return res.headers["Location"]

    def _home(self, login):
        """Where the user lands after logging in (following our redirects)."""
        location = self._login(login)
        for _hop in range(3):
            path = "/" + location.split("/", 3)[-1] if location.startswith("http") else location
            res = self.url_open(path, allow_redirects=False)
            if res.status_code not in (301, 302, 303):
                return path
            location = res.headers["Location"]
        return location

    def test_every_packer_and_driver_lands_on_inicio(self):
        for user in (self.packer, self.driver, self.simon, self.italo, self.admin):
            self.assertEqual(user._distri_landing(), "", user.login)
            self.assertTrue(user._distri_has_inicio(), user.login)
            self.assertEqual(user._distri_ajustes()["inicio"], "/tablero", user.login)
            self.assertTrue(self._home(user.login).endswith("/tablero"), user.login)
        # a seller locked to the accounts app keeps /cuentas, without Inicio
        self.assertEqual(self.seller._distri_landing(), "/cuentas")
        self.assertFalse(self.seller._distri_has_inicio())
        self.assertEqual(self.seller._distri_ajustes()["inicio"], "")
        self.assertTrue(self._home(self.seller.login).endswith("/cuentas"))
        # without the lock the seller is staff with Inicio too
        self.env["ir.config_parameter"].sudo().set_param(LOCK, "0")
        self.assertEqual(self.seller._distri_landing(), "")
        self.assertTrue(self._home(self.seller.login).endswith("/tablero"))

    def test_locked_seller_who_delivers_still_locked_out_of_the_backend(self):
        self._login(self.italo.login)
        res = self.url_open("/odoo/action-base.action_res_users", allow_redirects=False)
        self.assertIn(res.status_code, (301, 302, 303))
        self.assertTrue(res.headers["Location"].endswith("/cuentas"))
        page = self.url_open("/tablero")
        self.assertEqual(page.status_code, 200)
        for href in ('href="/reparto"', 'href="/armado"', 'href="/cuentas"', 'href="/pedido"'):
            self.assertIn(href, page.text)
        # no tile nor app that would only bounce him to /cuentas
        self.assertNotIn('href="/odoo', page.text)
        self.assertNotIn("Todas las aplicaciones", page.text)

    def test_tiles_of_each_role(self):
        self._login(self.packer.login)
        page = self.url_open("/tablero").text
        self.assertIn('href="/armado"', page)
        self.assertNotIn('href="/reparto"', page)
        self._login(self.simon.login)
        page = self.url_open("/tablero").text
        self.assertIn('href="/armado"', page)
        self.assertIn('href="/reparto"', page)
        self.assertIn('href="/cuentas"', page)

    def test_way_back_to_inicio_on_every_staff_screen(self):
        screens = {self.driver: ("/reparto", "/cuentas", "/cobro"),
                   self.packer: ("/armado", "/cobro"),
                   self.simon: ("/reparto", "/armado", "/cuentas", "/cobro", "/comprobantes")}
        for user, urls in screens.items():
            self._login(user.login)
            for url in urls:
                res = self.url_open(url)
                self.assertEqual(res.status_code, 200, (user.login, url))
                self.assertIn('href="/tablero"', res.text, (user.login, url))
                self.assertIn("Inicio: todas tus pantallas", res.text, (user.login, url))
        # the floating button on the single-page apps (gear_float)
        self._login(self.driver.login)
        self.assertIn('id="dn-home"', self.url_open("/reparto").text)
        # not on Inicio itself
        self.assertNotIn('id="dn-home"', self.url_open("/tablero").text)
        # the locked seller has /cuentas as his only screen: no Inicio button
        self._login(self.seller.login)
        page = self.url_open("/cuentas").text
        self.assertNotIn('id="dn-home"', page)
        self.assertNotIn('href="/tablero"', page)


@tagged("post_install", "-at_install", "distri_v120", "distri_v120_mayoristas")
class TestV120Mayoristas(RepartoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.admin = _user(env, "denis_may_v120", "distri_pedido.grupo_administracion")
        cls.driver = _user(env, "simon_may_v120",
                           "distri_pedido.grupo_reparto,distri_pedido.grupo_armador")
        cls.packer = _user(env, "facu_may_v120", "distri_pedido.grupo_armador")
        cls.seller = _user(env, "vende_may_v120", "distri_pedido.grupo_vendedor")
        cls.product = env["product.product"].create({
            "name": "Producto mayorista v120", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.wholesale = env["res.partner"].create({
            "name": "PEPE MAYORISTA V120", "customer_rank": 1, "company_type": "company",
            "distri_es_mayorista": True, "street": "Calle Mayorista 120",
            "city": "Neuquén", "phone": "299 455 1200"})
        cls.direct = env["res.partner"].create({
            "name": "KIOSCO DIRECTO V120", "customer_rank": 1, "company_type": "company",
            "street": "Calle Directa 120", "city": "Neuquén", "phone": "299 455 1201"})
        env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": cls.wholesale.id, "fecha": "2026-11-01",
            "comprobante": "0001/2026/12001", "tipo": "factura", "debe": 50000.0})

    def _planned(self):
        """A wholesale and a direct order planned by administración on /rutas
        into the driver's route, which leaves the depot."""
        Route = self.env["distri.route"].with_user(self.admin)
        orders = self._order(self.wholesale, self.product, qty=3) \
            | self._order(self.direct, self.product, qty=2)
        board = Route._planning_board()
        free = {c["id"] for g in board["groups"] for c in g["orders"]}
        self.assertLessEqual(set(orders.ids), free, "both in «Sin ruta»")
        route = Route.planning_create(today_ar(), "1", self.driver)
        for order in orders:
            Route.planning_move(order.with_user(self.admin), route.with_user(self.admin))
        self.assertEqual(route.sudo().stop_ids.order_id, orders)
        self._on_road(route)
        stops = route.sudo().stop_ids
        return (route.sudo(), stops.filtered(lambda s: s.partner_id == self.wholesale),
                stops.filtered(lambda s: s.partner_id == self.direct))

    # ------------------------------------------------------------ administración

    def test_admin_sees_wholesale_customers_and_their_balance_in_cuentas(self):
        App = self.env["distri.account.app"]
        data = App.with_user(self.admin).app_debtors(settled=True)
        debtors = {d["id"]: d for d in data["debtors"]}
        self.assertIn(self.wholesale.id, debtors)
        self.assertEqual(debtors[self.wholesale.id]["balance"], 50000.0)
        account = App.with_user(self.admin).app_account(self.wholesale.id)
        self.assertTrue(account)
        # sellers and drivers still never reach them
        for user in (self.seller, self.driver):
            data = App.with_user(user).app_debtors(settled=True)
            ids = {d["id"] for key in ("debtors", "settled", "uncollectible") for d in data[key]}
            self.assertNotIn(self.wholesale.id, ids, user.login)
            self.assertIn(self.direct.id, ids, user.login)

    def test_admin_plans_and_prints_the_route_with_a_wholesale_order(self):
        route, stop, _direct = self._planned()
        html = self.env["ir.actions.report"].with_user(self.admin)._render_qweb_html(
            "distri_pedido.report_hoja_ruta", route.ids)[0].decode()
        self.assertIn(self.wholesale._distri_app_name(), html)
        self.assertIn("Calle Mayorista 120", html)
        self.assertIn("debía $ 50.000", html)

    # ------------------------------------------------------------ driver

    def test_driver_gets_the_wholesale_stop_and_delivers_it(self):
        route, stop, direct = self._planned()
        self.authenticate(self.driver.login, self.driver.login)
        data = self.url_open("/reparto/api/route?id=%d" % route.id).json()
        self.assertTrue(data["ok"], data)
        stops = {s["id"]: s for s in data["route"]["stops"]}
        self.assertIn(stop.id, stops)
        got = stops[stop.id]
        self.assertEqual(got["name"], self.wholesale._distri_app_name())
        self.assertIn("Calle Mayorista 120", got["address"])
        self.assertTrue(got["whatsapp"])
        self.assertEqual(got["order_amount"], 300.0)
        self.assertEqual(got["previous_debt"], stop.previous_debt)
        self.assertIs(got["account"], False)
        self.assertIs(stops[direct.id]["account"], True)
        # he delivers and collects it like any other stop
        res = self._sync([self._event(route, "visit", self._visit(stop, cash=300))],
                         user=self.driver)
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(stop.state, "paid")
        self.assertEqual(stop.cash_amount, 300)

    def test_driver_never_sees_the_wholesale_account(self):
        route, stop, direct = self._planned()
        self.authenticate(self.driver.login, self.driver.login)
        res = self.url_open("/reparto/api/account?stop=%d" % stop.id)
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("50000", res.text)
        self.assertIn("administración", res.json()["error"])
        res = self.url_open("/reparto/api/account?stop=%d" % direct.id)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        # nor through /cuentas or /cobro
        App = self.env["distri.account.app"]
        self.assertFalse(App.with_user(self.driver)._app_allowed(self.wholesale))
        names = [r["name"] for r in self.env["distri.cash.collection"].with_user(
            self.driver).cash_search("V120")]
        self.assertNotIn(self.wholesale.name, names)

    def test_packer_packs_wholesale_orders_as_before(self):
        order = self._order(self.wholesale, self.product, qty=1)
        order.sudo().write({"distri_armado": "pendiente"})
        self.authenticate(self.packer.login, self.packer.login)
        res = self.url_open("/armado")
        self.assertEqual(res.status_code, 200)
        self.assertIn(order.name, res.text)
