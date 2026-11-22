import json

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin


@tagged("post_install", "-at_install", "distri_despacho")
class TestDespacho(RepartoMixin, HttpCase):
    """«Despacho» (models/despacho.py): every order checked by the driver and
    by the depot before the route leaves."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        company = cls.env.company

        def user(login, name, groups):
            return new_test_user(cls.env, login=login, name=name, groups=groups,
                                 company_id=company.id, company_ids=[Command.set(company.ids)])

        cls.dispatcher = user("despachante", "Despachante de Práctica",
                              "base.group_user,distri_pedido.grupo_administracion")
        cls.manager = user("gerente_despacho", "Gerente de Práctica",
                           "base.group_user,distri_pedido.grupo_gerencia")
        cls.seller = user("vendedor_despacho", "Vendedor de Práctica",
                          "base.group_user,distri_pedido.grupo_vendedor")
        cls.admin_driver = user("admin_repartidor", "Admin Repartidor",
                                "base.group_user,distri_pedido.grupo_administracion")
        Zone = cls.env["distri.zona"]
        cls.zone_a = Zone.create({"name": "Zona Práctica Norte"})
        cls.zone_b = Zone.create({"name": "Zona Práctica Sur"})
        product = cls.env["product.product"].create({
            "name": "Producto despacho", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.orders = cls.env["sale.order"]
        # the zone comes from the customer's city (armado.py)
        for name, zone in (("DESPENSA DE PRACTICA", cls.zone_a), ("KIOSCO DE PRACTICA", cls.zone_a),
                           ("ALMACEN DE PRACTICA", cls.zone_b)):
            partner = cls.env["res.partner"].create({
                "name": name, "street": "Calle Falsa 1", "city": zone.name})
            cls.orders |= cls._order(partner, product, qty=2, price=100)

    def setUp(self):
        super().setUp()
        self.route = self._route(self.orders)
        self.s1, self.s2, self.s3 = self.route.stop_ids

    def _load(self, *stops, loaded=True):
        return self._sync([self._event(self.route, "load", {"stop_id": s.id, "loaded": loaded})
                           for s in stops])

    def _as(self, user, records):
        return records.with_user(user).sudo(False)

    # ------------------------------------------------------------ permissions

    def test_only_admin_and_manager_other_than_the_driver_check(self):
        stop = self._as(self.dispatcher, self.s1)
        stop.action_dispatch_check()
        self.assertTrue(self.s1.dispatch_checked)
        self.assertEqual(self.s1.dispatch_checked_by_id, self.dispatcher)
        self.assertTrue(self.s1.dispatch_checked_at)
        self._as(self.manager, self.s2).action_dispatch_check()
        self.assertEqual(self.s2.dispatch_checked_by_id, self.manager)
        # the driver (no admin group) and a seller are refused
        for user in (self.driver, self.seller):
            with self.assertRaises(UserError):
                self._as(user, self.s3).action_dispatch_check()
        self.assertFalse(self.s3.dispatch_checked)
        # 19.0.1.127.0: an admin who drives this route may check his own load
        # (autodespacho, tests/test_v127_paquete.py); a plain driver may not
        own = self._route(self._order(self.s1.partner_id, self.s1.order_id.order_line.product_id),
                          driver=self.admin_driver)
        self._as(self.admin_driver, own.stop_ids).action_dispatch_check()
        self.assertTrue(own.stop_ids.dispatch_checked)
        with self.assertRaisesRegex(UserError, "otra persona"):
            self.route.stop_ids[:1].with_user(self.driver).sudo().action_dispatch_check()

    def test_despacho_page_and_api_only_for_admin(self):
        self.authenticate(self.driver.login, self.driver.login)
        self.assertEqual(self.url_open("/despacho").status_code, 403)
        self.assertEqual(self.url_open("/despacho/api/routes").status_code, 403)
        res = self.url_open("/despacho/api/action", data=json.dumps({
            "action": "check", "route_id": self.route.id, "stop_id": self.s1.id}),
            headers={"Content-Type": "application/json"})
        self.assertEqual(res.status_code, 403)
        self.assertFalse(self.s1.dispatch_checked)

        self.authenticate(self.seller.login, self.seller.login)
        self.assertEqual(self.url_open("/despacho").status_code, 403)

        self.authenticate(self.dispatcher.login, self.dispatcher.login)
        page = self.url_open("/despacho")
        self.assertEqual(page.status_code, 200)
        self.assertIn("despacho.js", page.text)
        data = self.url_open("/despacho/api/routes").json()
        self.assertIn(self.route.id, [r["id"] for r in data["routes"]])
        res = self.url_open("/despacho/api/action", data=json.dumps({
            "action": "check", "route_id": self.route.id, "stop_id": self.s1.id}),
            headers={"Content-Type": "application/json"}).json()
        self.assertTrue(res["ok"], res)
        self.assertTrue(self.s1.dispatch_checked)
        self.assertEqual(res["route"]["orders_checked"], 0)
        # a form post (no JSON) is refused
        res = self.url_open("/despacho/api/action", data={"action": "check"})
        self.assertEqual(res.status_code, 415)

    # ---------------------------------------------------------------- states

    def test_mismatch_states_and_texts(self):
        self._load(self.s1, self.s3)
        self._as(self.dispatcher, self.s2 | self.s3).action_dispatch_check()
        self.assertEqual(self.s1.load_state, "driver")
        self.assertEqual(self.s2.load_state, "dispatch")
        self.assertEqual(self.s3.load_state, "both")
        self.assertEqual(self.route.checked_stop_count, 1)
        self.assertEqual(self.route.parcel_checked_count, 2)
        self.assertTrue(self.route.dispatch_missing)

        payload = self._as(self.dispatcher, self.route)._despacho_payload()
        self.assertEqual([z["name"] for z in payload["zones"]],
                         ["Zona Práctica Norte", "Zona Práctica Sur"])
        rows = {r["id"]: r for z in payload["zones"] for r in z["stops"]}
        driver = "Repartidor"  # first name of «Repartidor Test»
        self.assertEqual(rows[self.s1.id]["message"], "%s lo tildó, vos todavía no" % driver)
        self.assertTrue(rows[self.s1.id]["mismatch"])
        self.assertEqual(rows[self.s2.id]["message"], "Vos lo tildaste, falta %s" % driver)
        self.assertTrue(rows[self.s2.id]["mismatch"])
        self.assertFalse(rows[self.s3.id]["mismatch"])
        self.assertEqual(payload["orders_checked"], 1)
        self.assertEqual(payload["orders_total"], 3)
        self.assertEqual(len(payload["missing"]), 2)
        # another dispatcher sees who checked it
        other = self._as(self.manager, self.route)._despacho_payload()
        rows = {r["id"]: r for z in other["zones"] for r in z["stops"]}
        self.assertEqual(rows[self.s2.id]["message"], "Despachante lo tildó, falta %s" % driver)
        # the driver's phone sees the depot's tick
        status = self.route._load_status_payload()
        by_id = {s["id"]: s for s in status["stops"]}
        self.assertTrue(by_id[self.s2.id]["dispatch_checked"])
        self.assertEqual(by_id[self.s2.id]["dispatch_by"], "Despachante")
        # unticking keeps the rest
        self._as(self.dispatcher, self.s3).action_dispatch_uncheck()
        self.assertEqual(self.s3.load_state, "driver")
        self.assertFalse(self.s3.dispatch_checked_by_id)

    # ---------------------------------------------------------------- depart

    def test_depart_rejected_until_every_order_is_double_checked(self):
        self._load(self.s1, self.s2, self.s3)
        res = self._sync([self._event(self.route, "depart", {})])
        self.assertEqual(res[0]["status"], "error")
        self.assertFalse(res[0]["retry"])
        self.assertIn("Todavía no podés salir", res[0]["error"])
        self.assertIn(self.s1.order_id.name, res[0]["error"])
        self.assertEqual(self.route.state, "draft")
        self.assertFalse(self.route.departed_at)

        self._as(self.dispatcher, self.s1 | self.s2).action_dispatch_check()
        res = self._sync([self._event(self.route, "depart", {})])
        self.assertEqual(res[0]["status"], "error")
        self.assertIn("1 de 3", res[0]["error"])
        self.assertEqual(self.route.state, "draft")

        self._as(self.dispatcher, self.s3).action_dispatch_check()
        res = self._sync([self._event(self.route, "depart", {})])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(self.route.state, "on_route")
        # once on the road the depot cannot touch the check anymore
        with self.assertRaisesRegex(UserError, "ya salió"):
            self._as(self.dispatcher, self.s1).action_dispatch_uncheck()

    def test_depart_with_an_unticked_order_after_a_double_check(self):
        """The driver unticks after the depot checked: back to missing."""
        self._load(self.s1, self.s2, self.s3)
        self._as(self.dispatcher, self.route.stop_ids).action_dispatch_check()
        self._load(self.s2, loaded=False)
        self.assertEqual(self.s2.load_state, "dispatch")
        res = self._sync([self._event(self.route, "depart", {})])
        self.assertEqual(res[0]["status"], "error")
        self.assertIn("falta Repartidor", res[0]["error"])

    # ------------------------------------------------------- loading order

    def test_loading_order_and_reverse_sequence_only_on_confirm(self):
        self._sync([
            self._event(self.route, "load", {"stop_id": self.s2.id, "loaded": True},
                        captured="2026-09-27T11:00:00.000Z"),
            self._event(self.route, "load", {"stop_id": self.s3.id, "loaded": True},
                        captured="2026-09-27T11:01:00.000Z"),
            self._event(self.route, "load", {"stop_id": self.s1.id, "loaded": True},
                        captured="2026-09-27T11:02:00.000Z"),
        ])
        self.assertEqual([self.s2.load_sequence, self.s3.load_sequence, self.s1.load_sequence],
                         [1, 2, 3])
        self.assertTrue(self.s2.loaded_at)
        # a repeated tick keeps its place; an untick frees it
        self._load(self.s2)
        self.assertEqual(self.s2.load_sequence, 1)
        before = self.route.stop_ids.sorted("sequence")
        self.assertEqual(before, self.s1 | self.s2 | self.s3)
        self._as(self.dispatcher, self.route.stop_ids).action_dispatch_check()

        # without the driver's confirmation nothing is reordered
        other = self._route(self._order(self.s1.partner_id, self.s1.order_id.order_line.product_id)
                            | self._order(self.s2.partner_id, self.s1.order_id.order_line.product_id))
        o1, o2 = other.stop_ids.sorted("sequence")
        self._sync([self._event(other, "load", {"stop_id": o2.id, "loaded": True}),
                    self._event(other, "load", {"stop_id": o1.id, "loaded": True})])
        other.stop_ids.sudo().action_dispatch_check()
        self._depart(other)
        self.assertEqual(other.stop_ids.sorted("sequence"), o1 | o2)
        self.assertEqual(other.stop_ids.sorted("sequence")[0], o1)

        # with it: last loaded, first visited
        self._depart(self.route, reverse_load_order=True)
        order = self.route.stop_ids.sorted("sequence")
        self.assertEqual(order.ids, [self.s1.id, self.s3.id, self.s2.id])
        self.assertTrue(any("al revés de la carga" in (m.body or "") for m in self.route.message_ids))

    # -------------------------------------------------------- left at depot

    def test_left_at_depot_frees_order_and_is_not_counted(self):
        stop = self._as(self.dispatcher, self.s2)
        with self.assertRaises(UserError):
            self._as(self.driver, self.s2).action_leave_at_depot("No entra")
        order = self.s2.order_id
        left = stop.action_leave_at_depot("No entra en la camioneta")
        self.assertEqual(left.reason, "No entra en la camioneta")
        self.assertEqual(left.user_id, self.dispatcher)
        self.assertNotIn(order, self.route.stop_ids.order_id)
        self.assertEqual(self.route.stop_count, 2)
        self.assertEqual(self.route.parcel_count, 4)
        self.assertEqual(self.route.left_order_count, 1)
        # free again: another route can take it, and so can the depot
        self.assertTrue(self.route._order_is_free(order))
        self.assertEqual(order.distri_delivery_state, "unassigned")
        self.assertIn("queda en el depósito", self.route.message_ids[0].body)
        self.assertIn("No entra en la camioneta", order.message_ids[0].body)
        # «Asignar pedidos» of this same route does not bring it back
        self.route.zone_ids = self.zone_a | self.zone_b
        self.route.action_assign_orders()
        self.assertNotIn(order, self.route.stop_ids.order_id)
        # a late tick from the phone for the removed stop is not an error
        res = self._sync([self._event(self.route, "load", {"stop_id": left.stop_ref, "loaded": True})])
        self.assertEqual(res[0]["status"], "ok", res)

        # the rest double-checked is enough to leave and to control
        self._load(self.s1, self.s3)
        self._as(self.dispatcher, self.s1 | self.s3).action_dispatch_check()
        self.assertFalse(self.route.dispatch_missing)
        self._as(self.dispatcher, self.route).action_mark_controlled()
        body = self.route.message_ids[0].body
        self.assertIn("2 pedidos y 4 bultos con doble control", body)
        self.assertIn("No entra en la camioneta", body)

    def test_left_at_depot_undo(self):
        left = self._as(self.dispatcher, self.s3).action_leave_at_depot("Falta mercadería")
        order = left.order_id
        self._as(self.dispatcher, left).action_undo()
        self.assertIn(order, self.route.stop_ids.order_id)
        self.assertFalse(self.route.left_order_ids)
        back = self.route.stop_ids.filtered(lambda s: s.order_id == order)
        self.assertEqual(back.zone_id, self.zone_b)
        self.assertEqual(back.load_state, "none")

        # not when the order went to another route meanwhile
        left = self._as(self.dispatcher, back).action_leave_at_depot("Falta mercadería")
        other = self._route(order, driver=self.other_driver)
        self.assertIn(order, other.stop_ids.order_id)
        with self.assertRaisesRegex(UserError, "ya está en"):
            self._as(self.dispatcher, left).action_undo()

    # ------------------------------------------------------------ controlled

    def test_controlled_blocked_with_missing_and_allowed_when_complete(self):
        self._load(self.s1, self.s2)
        self._as(self.dispatcher, self.s1).action_dispatch_check()
        with self.assertRaises(UserError) as err:
            self._as(self.dispatcher, self.route).action_mark_controlled()
        text = str(err.exception)
        self.assertIn("No se puede dar por controlada", text)
        self.assertIn("2 de 3", text)
        self.assertIn("%s · Kiosco De Practica: falta el depósito" % self.s2.order_id.name, text)
        self.assertIn("faltan los dos", text)
        self.assertFalse(self.route.departure_controlled_at)

        self._load(self.s3)
        self._as(self.dispatcher, self.s2 | self.s3).action_dispatch_check()
        self._as(self.dispatcher, self.route).action_mark_controlled()
        self.assertEqual(self.route.departure_controlled_by_id, self.dispatcher)
        self.assertIn("3 pedidos y 6 bultos con doble control", self.route.message_ids[0].body)
        # closed: no more changes from the depot, and not twice
        with self.assertRaisesRegex(UserError, "ya se controló"):
            self._as(self.dispatcher, self.s1).action_dispatch_uncheck()
        with self.assertRaisesRegex(UserError, "ya se controló"):
            self._as(self.dispatcher, self.route).action_mark_controlled()

    def test_load_ticks_locked_after_controlled(self):
        """After «Controlado» the driver can't change the load, but can leave."""
        self._load(self.s1, self.s2, self.s3)
        self._as(self.dispatcher, self.route.stop_ids).action_dispatch_check()
        self._as(self.dispatcher, self.route).action_mark_controlled()
        res = self._load(self.s2, loaded=False)
        self.assertEqual(res[0]["status"], "error")
        self.assertFalse(res[0]["retry"])
        self.assertEqual(res[0]["error"], "La carga ya fue controlada con Administración.")
        self.assertTrue(self.s2.loaded)
        self.assertEqual(self.s2.load_state, "both")
        # the same tick again is harmless
        self.assertEqual(self._load(self.s2)[0]["status"], "ok")
        # the phone learns it with the route
        self.assertTrue(self.route._app_payload()["controlled"])
        self.assertTrue(self.route._load_status_payload()["controlled"])
        res = self._sync([self._event(self.route, "depart", {})])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(self.route.state, "on_route")

    def test_controlled_through_the_api(self):
        self.authenticate(self.dispatcher.login, self.dispatcher.login)

        def act(**body):
            return self.url_open("/despacho/api/action", data=json.dumps(
                dict(body, route_id=self.route.id)),
                headers={"Content-Type": "application/json"}).json()

        res = act(action="controlled")
        self.assertFalse(res["ok"])
        self.assertIn("No se puede dar por controlada", res["error"])
        # 19.0.1.119.0: «Bajar al depósito» is one tap, no reason typed
        res = act(action="leave", stop_id=self.s3.id)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["route"]["left"][0]["reason"], "Bajado en el despacho")
        self._load(self.s1, self.s2)
        for stop in (self.s1, self.s2):
            self.assertTrue(act(action="check", stop_id=stop.id)["ok"])
        res = act(action="controlled")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["route"]["controlled"])
        self.assertFalse(res["route"]["editable"])

    def test_phone_payload_carries_both_ticks(self):
        self._as(self.dispatcher, self.s1).action_dispatch_check()
        self.authenticate(self.driver.login, self.driver.login)
        data = self.url_open("/reparto/api/load?id=%s" % self.route.id).json()
        self.assertTrue(data["ok"], data)
        by_id = {s["id"]: s for s in data["route"]["stops"]}
        self.assertTrue(by_id[self.s1.id]["dispatch_checked"])
        self.assertFalse(by_id[self.s2.id]["dispatch_checked"])
        route = self.url_open("/reparto/api/route?id=%s" % self.route.id).json()["route"]
        self.assertIn("dispatch_checked", route["stops"][0])
        self.assertEqual(route["left"], [])
        # another driver's route is not visible
        self.authenticate(self.other_driver.login, self.other_driver.login)
        self.assertEqual(self.url_open("/reparto/api/load?id=%s" % self.route.id).status_code, 404)
