"""19.0.1.119.0: orders of the same customer travel together on the routes,
each with its own invoice (never merged).

- /rutas, «Sin ruta»: the customer's orders side by side, with the chip
  «N pedidos de este cliente» and the delivery address on every card.
- Moving one order moves the customer's other orders that may move on their
  own (into a route, back to the depot, between routes); held, unpacked or
  loaded ones stay.
- A route whose customer has another open order off it is warned (toast,
  column), never blocked; /despacho asks before «Controlado».
- /despacho: «Bajar al depósito» is one tap, without a reason to type.
- /proyecto and /presentacion* for internal users only, the «Capacitación»
  tile on Inicio and «Ir a practicar».
"""
import json
from urllib.parse import quote

from odoo import fields
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.tablero import staff_tiles
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_v110_planificacion import (
    CSRF_RE, FETCH, PlanningMixin)


@tagged("post_install", "-at_install", "distri_v119", "distri_v119_mismo_cliente")
class TestMismoCliente(PlanningMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_planning()
        cls.customer = cls.env["res.partner"].create({
            "name": "DESPENSA MISMO CLIENTE", "street": "Av. Argentina 1234",
            "city": cls.zone_a.name})
        cls.other = cls.env["res.partner"].create({
            "name": "KIOSCO OTRO CLIENTE", "street": "Calle Otra 5", "city": cls.zone_a.name})

    def setUp(self):
        super().setUp()
        self.o1, self.o2, self.o3 = self.orders
        # created in this order: a1, x, a2 (same customer, not consecutive)
        self.a1 = self._order(self.customer, self.product, qty=1, price=100)
        self.x = self._order(self.other, self.product, qty=1, price=100)
        self.a2 = self._order(self.customer, self.product, qty=1, price=100)
        self.r1 = self._new_route(self.driver)
        self.r2 = self._new_route(self.other_driver)

    def _post(self, action, data):
        csrf = CSRF_RE.search(self.url_open("/rutas").text).group(1)
        data = dict(data, csrf_token=csrf, fecha=fields.Date.to_string(today_ar()))
        return self.url_open("/rutas/accion/%s" % action, data=data, headers=FETCH)

    def _client(self):
        return self.customer._distri_app_name()

    def _orders_on(self, route):
        return route.stop_ids.sorted("sequence").order_id

    # ------------------------------------------------------------ depot

    def test_depot_shows_same_customer_together_with_address(self):
        board = self._planner()._planning_board()
        group = next(g for g in board["groups"]
                     if any(c["id"] == self.a1.id for c in g["orders"]))
        ids = [c["id"] for c in group["orders"]]
        self.assertEqual(ids.index(self.a2.id), ids.index(self.a1.id) + 1,
                         "the customer's orders are side by side")
        self.assertLess(ids.index(self.a2.id), ids.index(self.x.id))
        cards = {c["id"]: c for c in group["orders"]}
        for order in (self.a1, self.a2):
            self.assertIn(("2 pedidos de este cliente", "neutral"), cards[order.id]["chips"])
            self.assertEqual(cards[order.id]["address"], "Av. Argentina 1234, %s" % self.zone_a.name)
        self.assertFalse(any("pedidos de este cliente" in c[0] for c in cards[self.x.id]["chips"]))
        self.assertEqual(cards[self.x.id]["address"], "Calle Otra 5, %s" % self.zone_a.name)
        self.authenticate(self.admin.login, self.admin.login)
        html = self.url_open("/rutas/tablero").json()["html"]
        self.assertIn("2 pedidos de este cliente", html)
        self.assertIn("Av. Argentina 1234", html)

    # ------------------------------------------------------------ moves

    def test_move_in_pulls_the_customers_free_orders_right_after_it(self):
        self._move(self.o1, self.r1)
        self._move(self.o2, self.r1)
        first = self.r1.stop_ids.filtered(lambda s: s.order_id == self.o2)
        a3 = self._order(self.customer, self.product, qty=1, price=100)
        self.authenticate(self.admin.login, self.admin.login)
        res = self._post("mover", {"order_id": self.a1.id, "route_id": self.r1.id,
                                   "before_id": first.id}).json()
        self.assertTrue(res["ok"], res)
        self.assertEqual(self._orders_on(self.r1).ids,
                         [self.o1.id, self.a1.id, self.a2.id, a3.id, self.o2.id])
        self.assertIn("También se movieron %s, %s." % (self.a2.name, a3.name), res["message"])
        self.assertNotIn("Ojo:", res["message"], "nothing of the customer is left behind")
        self.assertNotIn(self.x, self.r1.stop_ids.order_id)

    def test_move_out_and_between_routes_moves_the_group(self):
        self._move(self.a1, self.r1)
        self.assertEqual(self._orders_on(self.r1), self.a1 | self.a2)
        # between routes: the group goes together
        self.authenticate(self.admin.login, self.admin.login)
        res = self._post("mover", {"order_id": self.a2.id, "route_id": self.r2.id}).json()
        self.assertTrue(res["ok"], res)
        self.assertFalse(self.r1.stop_ids)
        self.assertEqual(self._orders_on(self.r2), self.a2 | self.a1)
        self.assertIn("También se movió %s." % self.a1.name, res["message"])
        # back to the depot: the whole group goes back
        res = self._post("mover", {"order_id": self.a1.id, "route_id": 0}).json()
        self.assertTrue(res["ok"], res)
        self.assertFalse(self.r2.stop_ids)
        self.assertIn("%s vuelve al depósito. También se movió %s." % (self.a1.name, self.a2.name),
                      res["message"])
        # unless one is loaded (physically on the truck): that one stays
        self._move(self.a1, self.r2)
        self.assertEqual(self._orders_on(self.r2), self.a1 | self.a2)
        loaded = self.r2.stop_ids.filtered(lambda s: s.order_id == self.a1)
        self._sync([self._event(self.r2, "load", {"stop_id": loaded.id, "loaded": True})],
                   user=self.other_driver)
        res = self._post("mover", {"order_id": self.a2.id, "route_id": 0}).json()
        self.assertTrue(res["ok"], res)
        self.assertEqual(self._orders_on(self.r2), self.a1)
        self.assertNotIn("También", res["message"])
        # the one moved out is now warned on the route it left
        self.assertIn("Ojo: %s tiene otro pedido (%s) armado que no está en ninguna ruta."
                      % (self._client(), self.a2.name), res["message"])

    def test_held_unpacked_and_left_siblings_stay(self):
        self.a2.with_user(self.manager).distri_retener("Debe plata")
        unpacked = self._order(self.customer, self.product, qty=1, price=100)
        unpacked.sudo().write({"distri_armado": "pendiente"})
        self._move(self.a1, self.r1)
        self.assertEqual(self._orders_on(self.r1), self.a1)
        warning = self.r1._planning_sibling_warning()
        self.assertIn("Ojo: %s tiene otro pedido (%s) armado pero retenido. "
                      "¿Lo liberás para cargarlo?" % (self._client(), self.a2.name), warning)
        self.assertIn("Ojo: %s tiene otro pedido (%s) sin armar. "
                      "¿Lo terminás de armar para cargarlo?" % (self._client(), unpacked.name),
                      warning)
        # an order the dispatcher left at the depot from this route is not
        # pulled back by its sibling, nor warned
        a3 = self._order(self.customer, self.product, qty=1, price=100)
        self._move(a3, self.r2)
        self._move(self.o1, self.r2)
        stop = self.r2.stop_ids.filtered(lambda s: s.order_id == a3)
        stop.with_user(self.admin).action_leave_at_depot()
        self._move(self.a1, self.r2)
        self.assertNotIn(a3, self.r2.stop_ids.order_id)
        self.assertNotIn(a3.name, self.r2._planning_sibling_warning())

    # ------------------------------------------------------------ warning

    def test_warning_for_unpacked_sibling_in_toast_and_column(self):
        self.a2.sudo().write({"distri_armado": "en_armado"})
        self.authenticate(self.admin.login, self.admin.login)
        res = self._post("mover", {"order_id": self.a1.id, "route_id": self.r1.id}).json()
        self.assertTrue(res["ok"], res)
        text = ("Ojo: %s tiene otro pedido (%s) sin armar. ¿Lo terminás de armar para cargarlo?"
                % (self._client(), self.a2.name))
        self.assertIn(text, res["message"])
        self.assertNotIn("También", res["message"])
        self.assertIn("rt-sib", res["html"])
        columns = {c["id"]: c for c in self._planner()._planning_board()["columns"]}
        self.assertEqual(columns[self.r1.id]["siblings"], [text])
        self.assertEqual(columns[self.r2.id]["siblings"], [])
        # never blocks: the route can take the next one
        self._move(self.o1, self.r1)

    def test_no_warning_when_the_customer_has_nothing_else_open(self):
        self.a2.sudo().write({"state": "cancel"})
        pickup = self._order(self.customer, self.product, qty=1, price=100)
        pickup.sudo().write({"distri_entrega": "retira", "distri_armado": "pendiente"})
        self._move(self.a1, self.r1)
        self.assertEqual(self._orders_on(self.r1), self.a1)
        self.assertEqual(self.r1._planning_sibling_notes(), [])
        self.assertEqual(self.r1._planning_sibling_warning(), "")
        self._move(self.o1, self.r2)
        self.assertEqual(self.r2._planning_sibling_notes(), [])

    # ------------------------------------------------------------ despacho

    def _act(self, route, **body):
        return self.url_open("/despacho/api/action", data=json.dumps(dict(body, route_id=route.id)),
                             headers={"Content-Type": "application/json"}).json()

    def test_controlled_asks_before_closing_with_pending_siblings(self):
        self.a2.sudo().write({"distri_armado": "pendiente"})
        self._move(self.a1, self.r1)
        stop = self.r1.stop_ids
        self.authenticate(self.admin.login, self.admin.login)
        # the existing refusal comes first, as before
        res = self._act(self.r1, action="controlled")
        self.assertFalse(res["ok"])
        self.assertIn("No se puede dar por controlada", res["error"])
        self.assertNotIn("confirm", res)
        self._sync([self._event(self.r1, "load", {"stop_id": stop.id, "loaded": True})])
        self.assertTrue(self._act(self.r1, action="check", stop_id=stop.id)["ok"])
        res = self._act(self.r1, action="controlled")
        self.assertFalse(res["ok"])
        self.assertEqual(res["confirm"], [
            "Ojo: %s tiene otro pedido (%s) sin armar. ¿Lo terminás de armar para cargarlo?"
            % (self._client(), self.a2.name)])
        self.assertFalse(res["route"]["controlled"])
        self.r1.invalidate_recordset()
        self.assertFalse(self.r1.departure_controlled_at, "nothing closed until confirmed")
        res = self._act(self.r1, action="controlled", confirmed=True)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["route"]["controlled"])

    def test_controlled_without_siblings_needs_no_confirmation(self):
        self._move(self.o1, self.r1)
        stop = self.r1.stop_ids
        self._sync([self._event(self.r1, "load", {"stop_id": stop.id, "loaded": True})])
        self.authenticate(self.admin.login, self.admin.login)
        self.assertTrue(self._act(self.r1, action="check", stop_id=stop.id)["ok"])
        res = self._act(self.r1, action="controlled")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["route"]["controlled"])

    def test_bajar_al_deposito_one_tap_and_undo(self):
        self._move(self.o1, self.r1)
        self._move(self.o2, self.r1)
        stop = self.r1.stop_ids.filtered(lambda s: s.order_id == self.o2)
        self.authenticate(self.admin.login, self.admin.login)
        res = self._act(self.r1, action="leave", stop_id=stop.id)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["route"]["left"][0]["reason"], "Bajado en el despacho")
        self.assertNotIn(self.o2, self.r1.stop_ids.order_id)
        cards = {c["id"] for g in self._planner()._planning_board()["groups"] for c in g["orders"]}
        self.assertIn(self.o2.id, cards, "back in the «Sin ruta» queue")
        self.assertTrue(any("queda en el depósito" in (m.body or "")
                            for m in self.r1.message_ids))
        res = self._act(self.r1, action="undo", left_id=res["route"]["left"][0]["id"])
        self.assertTrue(res["ok"], res)
        self.assertIn(self.o2, self.r1.stop_ids.order_id)
        # a reason given (the held-order path of models/revisar.py) is kept
        left = self.r1.stop_ids.filtered(lambda s: s.order_id == self.o2) \
            .with_user(self.admin).action_leave_at_depot("Retenido: Debe plata")
        self.assertEqual(left.reason, "Retenido: Debe plata")


@tagged("post_install", "-at_install", "distri_v119", "distri_v119_capacitacion")
class TestCapacitacionStaffOnly(HttpCase):
    """19.0.1.119.0: /proyecto and /presentacion* only for internal users,
    the «Capacitación» tile on Inicio and «Ir a practicar»."""

    PAGES = ("/proyecto", "/presentacion", "/presentacion/cobranza", "/presentacion/reparto",
             "/presentacion/presupuesto", "/presentacion/migracion")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 19.0.1.128.0: a role that opens /cobro
        cls.staff = new_test_user(cls.env, login="staff_v119", name="Staff V119",
                                  groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.portal = new_test_user(cls.env, login="portal_v119", name="Portal V119",
                                   groups="base.group_portal")

    def test_public_goes_to_login_with_the_page_to_come_back(self):
        for url in self.PAGES:
            res = self.url_open(url + "?a=1&b=x", allow_redirects=False)
            self.assertIn(res.status_code, (302, 303), url)
            location = res.headers["Location"]
            self.assertIn("/web/login?redirect=%s" % quote(url + "?a=1&b=x", safe=""),
                          location, url)
        res = self.url_open("/proyecto", allow_redirects=False)
        self.assertTrue(res.headers["Location"].endswith(
            "/web/login?redirect=%2Fproyecto"), res.headers["Location"])

    def test_portal_customer_never_sees_them(self):
        self.authenticate(self.portal.login, self.portal.login)
        for url in self.PAGES:
            res = self.url_open(url, allow_redirects=False)
            self.assertEqual(res.status_code, 404, url)
            self.assertNotIn("Capacitación Distri Neuquén", res.text, url)

    def test_internal_user_sees_them(self):
        self.authenticate(self.staff.login, self.staff.login)
        for url in self.PAGES:
            self.assertEqual(self.url_open(url).status_code, 200, url)
        # the kit assets stay public (no content of their own)
        self.authenticate(None, None)
        self.assertEqual(self.url_open("/dn/proyecto.css").status_code, 200)

    def test_tile_on_inicio_last_for_every_internal_user(self):
        tiles = staff_tiles(self.env(user=self.staff))
        self.assertEqual(tiles[-1]["key"], "capacitacion")
        self.assertEqual(tiles[-1]["url"], "/proyecto")
        self.assertEqual(tiles[-1]["title"], "Capacitación")
        self.authenticate(self.staff.login, self.staff.login)
        res = self.url_open("/tablero")
        self.assertEqual(res.status_code, 200)
        self.assertIn('href="/proyecto"', res.text)

    def test_gear_links_capacitacion_for_staff_only(self):
        self.assertTrue(self.staff._distri_ajustes()["capacitacion"])
        self.assertFalse(self.portal._distri_ajustes()["capacitacion"])
        self.authenticate(self.staff.login, self.staff.login)
        res = self.url_open("/cobro")
        self.assertEqual(res.status_code, 200)
        self.assertIn('<a href="/proyecto">Capacitación</a>', res.text)

    def test_ir_a_practicar_in_production_and_in_practice(self):
        self.authenticate(self.staff.login, self.staff.login)
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param("distri_practica.activo", False)
        body = self.url_open("/proyecto").text
        self.assertIn("Ir a practicar", body)
        self.assertRegex(body, r'<a class="practicar" href="https://practica\.distrineuquen\.com"'
                               r' target="_blank" rel="noopener">')
        Param.set_param("distri_practica.activo", "1")
        body = self.url_open("/proyecto").text
        self.assertRegex(body, r'<a class="practicar" href="/">Ir a practicar</a>')
        self.assertNotIn('href="https://practica.distrineuquen.com"', body)
