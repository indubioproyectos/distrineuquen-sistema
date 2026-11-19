"""19.0.1.113.0 (owner's tests of 18-19/11/2026): the driver app and the
routes.

- A1: /reparto/api/route?id=X for a route that is gone answers "reset" with
  the driver's current route, never a 404; the pending changes of the gone
  route are still applied or kept as rejections (distri.sync.rejection).
- A2: the phone's state is per user (app2.js, tests/data/reparto_v113.js) and
  the sync refuses a batch of another user.
- A3: «Tus rutas de hoy»: every open route of the driver, the one «En
  reparto» first; the second one opens without settling the first.
- A4: the planning board warns (never blocks) when a driver has two routes of
  the same date and trip.
- A5: «Anular ruta» undoes a route without money and refuses one with money.
- D: the retired cuentas.js / cuentas.css are referenced by nothing.
"""
import json
import os
import re
import shutil
import subprocess
from unittest import SkipTest

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers import cuentas as cuentas_controller
from odoo.addons.distri_pedido.controllers import reparto as reparto_controller
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin
from odoo.addons.distri_pedido.tests.test_v110_planificacion import FETCH, PlanningMixin

JSON = {"Content-Type": "application/json"}
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')


@tagged("post_install", "-at_install", "distri_v113", "distri_v113_reparto")
class TestRepartoV113(RepartoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.admin = new_test_user(cls.env, login="admin_v113", name="Administracion V113",
                                  groups="base.group_user,distri_pedido.grupo_administracion",
                                  **company)
        cls.seller = new_test_user(cls.env, login="vendedor_v113", name="Vendedor V113",
                                   groups="base.group_user,distri_pedido.grupo_vendedor",
                                   **company)
        cls.product = cls.env["product.product"].create({
            "name": "Producto v113", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.partners = cls.env["res.partner"].create([
            {"name": "KIOSCO V113 UNO", "city": "Centenario"},
            {"name": "KIOSCO V113 DOS", "city": "Centenario"},
            {"name": "KIOSCO V113 TRES", "city": "Centenario"},
        ])

    def setUp(self):
        super().setUp()
        self.o1, self.o2, self.o3 = [self._order(p, self.product, qty=2) for p in self.partners]

    def _get(self, url):
        return self.url_open(url).json()

    def _post_sync(self, events, **extra):
        return self.url_open("/reparto/api/sync", data=json.dumps(dict(extra, events=events)),
                             headers=JSON)

    # ------------------------------------------------------------ A1

    def test_a1_gone_route_resets_instead_of_404(self):
        current = self._route(self.o1)
        other = self._route(self.o2, driver=self.other_driver)
        mine_settled = self._route(self.o3)
        mine_settled.sudo().state = "settled"
        deleted = self._route(self.env["sale.order"])
        deleted_id = deleted.id
        deleted.sudo().unlink()
        self.authenticate(self.driver.login, self.driver.login)
        for gone in (deleted_id, other.id, mine_settled.id, 0, "abc"):
            res = self.url_open("/reparto/api/route?id=%s" % gone)
            self.assertEqual(res.status_code, 200, gone)
            data = res.json()
            self.assertTrue(data["ok"])
            self.assertTrue(data["reset"], gone)
            self.assertEqual(data["route"]["id"], current.id, "the current route instead")
            self.assertEqual([r["id"] for r in data["routes"]], [current.id])
        self.assertEqual(self._get("/reparto/api/route?id=%s" % deleted_id)["gone_id"], deleted_id)
        # the own route is not «gone»; a route closed by the driver and not
        # settled is still shown (the cash to hand in)
        data = self._get("/reparto/api/route?id=%s" % current.id)
        self.assertNotIn("reset", data)
        current.sudo().state = "closed"
        data = self._get("/reparto/api/route?id=%s" % current.id)
        self.assertEqual((data["route"]["id"], data["route"]["state"]), (current.id, "closed"))
        self.assertNotIn("reset", data)

    def test_a1_pending_changes_of_a_gone_route_end_as_rejections(self):
        route = self._route(self.o1, on_road=True)
        stop = route.stop_ids
        route_id = route.id
        event = self._event(route, "visit", self._visit(stop, outcome="not_delivered",
                                                        reason="Cerrado"))
        route.sudo().unlink()
        self.authenticate(self.driver.login, self.driver.login)
        res = self._post_sync([event], user_id=self.driver.id).json()
        self.assertEqual(res["results"][0]["status"], "error")
        self.assertFalse(res["results"][0].get("retry"))
        rejection = self.env["distri.sync.rejection"].sudo().search([("uuid", "=", event["uuid"])])
        self.assertEqual(rejection.state, "open")
        self.assertFalse(rejection.route_id)
        self.assertIn("no es tuya", rejection.error)
        self.assertTrue(route_id)

    # ------------------------------------------------------------ A2

    def test_a2_user_on_the_page_and_sync_of_another_user_refused(self):
        route = self._route(self.o1, on_road=True)
        self.authenticate(self.driver.login, self.driver.login)
        page = self.url_open("/reparto").text
        self.assertIn('data-uid="%s"' % self.driver.id, page)
        self.assertIn("app3.js?v=", page)  # 19.0.1.115.0
        self.assertIn("app2.js", reparto_controller.ASSETS)
        self.assertEqual(self._get("/reparto/api/route")["user_id"], self.driver.id)
        event = self._event(route, "visit", self._visit(route.stop_ids, outcome="not_delivered",
                                                        reason="Cerrado"))
        # the outbox of another user of this phone is never applied as this one
        res = self._post_sync([event], user_id=self.other_driver.id)
        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.json()["user_mismatch"])
        self.assertEqual(route.stop_ids.state, "pending")
        self.assertFalse(self.env["distri.sync.event"].sudo().search([("uuid", "=", event["uuid"])]))
        # its own events go through; the older app (no user_id) as before
        res = self._post_sync([event], user_id=self.driver.id).json()
        self.assertEqual(res["results"][0]["status"], "ok")
        self.assertEqual(route.stop_ids.state, "not_delivered")
        self.assertEqual(self._post_sync([event]).json()["results"][0]["status"], "duplicate")

    def test_a2_a3_app_logic_in_node(self):
        """app2.js itself (tests/data/reparto_v113.js): the user switch, the
        gone route, «Tus rutas de hoy» and the older app's state."""
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("Node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/reparto_v113.js"), root],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        # another user: their own (empty) state; the previous user's route and
        # pending change stay on the phone, unsent
        self.assertEqual(out["switch"], {"route": 20, "uid": 8, "outbox": 0, "sent": [],
                                         "kept": True, "previousKept": True, "current": 20,
                                         "lastUser": 8})
        # a cached shell of another user: the server refuses the batch (409)
        # and the app moves to the logged-in user, keeping the change
        self.assertEqual(out["cachedShell"], {"route": 30, "uid": 9, "kept": True,
                                              "mismatch": True})
        # a gone route: the current one opens, the change waits, is sent and
        # rejected (kept under the gone route), then the notice is dismissed
        self.assertEqual(out["gone"]["route"], 11)
        self.assertEqual(out["gone"]["pending"], 1)
        self.assertTrue(out["gone"]["banner"])
        self.assertEqual(out["gone"]["after"], {"pending": 0, "banner": True, "rejected": 1})
        self.assertEqual(out["gone"]["dismissed"], {"gone": None, "local": False})
        # two routes: the picker; each route keeps its visits and its outbox
        self.assertEqual((out["picker"]["first"], out["picker"]["bar"], out["picker"]["list"]),
                         (11, True, 2))
        self.assertEqual(out["picker"]["second"], {"route": 12, "visits": 0, "outbox": 1,
                                                   "outboxRoute": 11})
        self.assertEqual(out["picker"]["back"], {"route": 11, "visits": 1})
        self.assertEqual(out["picker"]["single"], "", "one route: no picker")
        # the older app's state: nothing sent before the server names the user
        self.assertEqual(out["legacy"], {"order": ["route", "sync:5"], "uid": 5, "oldKeys": False,
                                         "route": 40, "notice": "1", "outbox": 0})

    # ------------------------------------------------------------ A3

    def test_a3_two_routes_on_route_first(self):
        first = self._route(self.o1)                          # draft, older
        second = self._route(self.o2, on_road=True)           # on the road
        second.sudo().trip = "2"
        self.authenticate(self.driver.login, self.driver.login)
        data = self._get("/reparto/api/route")
        self.assertEqual([r["id"] for r in data["routes"]], [second.id, first.id])
        self.assertEqual(data["route"]["id"], second.id, "the one «En reparto» by default")
        row = data["routes"][0]
        self.assertEqual((row["state"], row["state_label"]), ("on_route", "En reparto"))
        self.assertIn("Viaje 2", row["title"])
        self.assertIn("1 pedido", row["detail"])
        # the other one opens without settling the first
        data = self._get("/reparto/api/route?id=%s" % first.id)
        self.assertEqual((data["route"]["id"], data["route"]["state"]), (first.id, "draft"))
        self.assertEqual(second.state, "on_route")
        # one route: one row (the app shows no picker)
        second.sudo().state = "settled"
        self.assertEqual(len(self._get("/reparto/api/route")["routes"]), 1)

    # ------------------------------------------------------------ A5

    def test_a5_cancel_route_without_money(self):
        route = self._route(self.o1 | self.o2, on_road=True)
        s1, s2 = route.stop_ids
        res = self._sync([self._event(route, "visit", self._visit(
            s1, outcome="not_delivered", reason="Prueba"))])
        self.assertEqual(res[0]["status"], "ok", res)
        # a change the server rejected, without money: resolved with the route
        bad = self._sync([self._event(route, "visit", {"stop_id": 0})])
        self.assertEqual(bad[0]["status"], "error")
        rejection = self.env["distri.sync.rejection"].sudo().search(
            [("route_id", "=", route.id), ("state", "=", "open")])
        self.assertTrue(rejection)
        name, route_id = route.name, route.id
        with self.assertRaisesRegex(UserError, "administración o gerencia"):
            route.with_user(self.seller).action_distri_cancel_route()
        # 19.0.1.115.0: a route «En reparto» is refused (reopen or settle it)
        with self.assertRaisesRegex(UserError, "ya salió a reparto.*Reabrir para el repartidor"):
            route.with_user(self.admin).action_distri_cancel_route()
        route.sudo().state = "draft"
        action = route.with_user(self.admin).action_distri_cancel_route()
        self.assertEqual(action["res_model"], "distri.route")
        self.assertFalse(route.exists())
        self.assertEqual(rejection.state, "resolved")
        self.assertFalse(rejection.route_id)
        self.assertIn(name, rejection.resolution)
        for order in (self.o1, self.o2):
            self.assertFalse(order.distri_stop_ids)
            self.assertTrue(any("Se anuló la ruta" in (m.body or "") for m in order.message_ids))
        self.assertTrue(any("se deshizo" in (m.body or "") for m in self.o1.message_ids))
        # free again: another route takes them
        other = self._route(self.o1 | self.o2, driver=self.other_driver)
        self.assertEqual(other.stop_ids.order_id, self.o1 | self.o2)
        # the driver's phone moves on (A1)
        self.authenticate(self.driver.login, self.driver.login)
        data = self._get("/reparto/api/route?id=%s" % route_id)
        self.assertEqual((data["ok"], data["reset"], data["gone_id"]), (True, True, route_id))

    def test_a5_cancel_route_with_money_is_refused(self):
        route = self._route(self.o1, on_road=True)
        res = self._sync([self._event(route, "visit", self._visit(route.stop_ids, cash=300))])
        self.assertEqual(res[0]["status"], "ok", res)
        with self.assertRaisesRegex(UserError, "ya salió a reparto"):
            route.with_user(self.admin).action_distri_cancel_route()
        route.sudo().state = "draft"
        with self.assertRaisesRegex(UserError, "tiene plata .*efectivo cobrado"):
            route.with_user(self.admin).action_distri_cancel_route()
        self.assertTrue(route.exists())
        self.assertEqual(route.stop_ids.state, "paid")
        settled = self._route(self.o2)
        settled.sudo().state = "settled"
        with self.assertRaisesRegex(UserError, "ya se rindió"):
            settled.with_user(self.admin).action_distri_cancel_route()
        # the backend button, for administración and gerencia
        arch = self.env.ref("distri_pedido.view_route_form").arch_db
        self.assertIn("action_distri_cancel_route", arch)
        self.assertIn("Anular ruta", arch)

    # ------------------------------------------------------------ D

    def test_d_old_cuentas_files_not_referenced(self):
        self.assertNotIn("cuentas.js", cuentas_controller.ASSETS)
        self.assertNotIn("cuentas.css", cuentas_controller.ASSETS)
        views = self.env["ir.ui.view"].sudo().search(
            ["|", ("arch_db", "ilike", "cuentas.js"), ("arch_db", "ilike", "cuentas.css")])
        self.assertFalse(views, views.mapped("key"))
        root = get_module_path("distri_pedido")
        for folder, _dirs, files in os.walk(os.path.join(root, "static")):
            for name in files:
                # the two retired files themselves leave with the install
                if name in ("cuentas.js", "cuentas.css"):
                    continue
                if name.endswith((".js", ".xml", ".html")):
                    with open(os.path.join(folder, name), encoding="utf-8") as handle:
                        text = handle.read()
                    self.assertIsNone(re.search(r"cuentas\.(js|css)", text), name)
        self.authenticate(self.admin.login, self.admin.login)
        page = self.url_open("/cuentas").text
        self.assertIn("cuentas_app.js", page)
        self.assertIsNone(re.search(r"/cuentas\.(js|css)", page))


@tagged("post_install", "-at_install", "distri_v113", "distri_v113_planificacion")
class TestPlanningTwinV113(PlanningMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_planning()

    def _post(self, action, data):
        csrf = CSRF_RE.search(self.url_open("/rutas").text).group(1)
        data = dict(data, csrf_token=csrf, fecha=fields.Date.to_string(today_ar()))
        return self.url_open("/rutas/accion/%s" % action, data=data, headers=FETCH)

    def test_a4_two_routes_same_day_and_trip_warned_not_blocked(self):
        first = self._new_route(self.driver)
        self.assertFalse(first._planning_twin_warning())
        # «Nueva ruta» with the same driver, day and trip: created, warned
        self.authenticate(self.admin.login, self.admin.login)
        res = self._post("nueva", {"driver_id": self.driver.id, "trip": "1"}).json()
        self.assertTrue(res["ok"], res)
        self.assertIn("Ojo: Repartidor ya tiene otra ruta del mismo día y viaje", res["message"])
        second = self.env["distri.route"].search(
            [("driver_id", "=", self.driver.id), ("date", "=", today_ar()), ("trip", "=", "1")],
            order="id desc", limit=1)
        self.assertNotEqual(second, first)
        self.assertIn(first._planning_title(), second._planning_twin_warning())
        # the column says it too, on the board
        self.assertIn("Ojo:", res["html"])
        columns = {c["id"]: c for c in self._planner()._planning_board()["columns"]}
        self.assertTrue(columns[first.id]["twin"])
        self.assertTrue(columns[second.id]["twin"])
        # picking the driver of an empty route: warned; trip 2 is not a twin
        empty = self._new_route()
        res = self._post("repartidor", {"route_id": empty.id, "driver_id": self.driver.id}).json()
        self.assertTrue(res["ok"], res)
        self.assertIn("otras 2 rutas", res["message"])
        trip2 = self._new_route(self.other_driver, trip="2")
        self.assertFalse(trip2._planning_twin_warning())
        # the backend form warns too (onchange), never blocks
        form_route = self.env["distri.route"].new({
            "kind": "route", "driver_id": self.driver.id, "date": today_ar(), "trip": "1"})
        warning = form_route._onchange_planning_twin()
        self.assertIn("Ojo", warning["warning"]["message"])
