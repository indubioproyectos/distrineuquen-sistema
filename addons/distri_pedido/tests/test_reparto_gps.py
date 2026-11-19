"""19.0.1.35.0: location of the driver's app at the key moments of a route
(models/reparto_gps.py, static/src/reparto/app2.js)."""
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from unittest import SkipTest

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.modules.module import get_module_path
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.reparto_gps import (
    NOTICE_VERSION, PARAM_ACCURACY, PARAM_DISTANCE, PARAM_REQUIRED, PARAM_RETENTION,
)

from .test_reparto import RepartoMixin

# a fictitious corner in Neuquén and points around it
HOME = (-38.951600, -68.059100)
NEAR = {"lat": -38.951650, "lng": -68.059150, "acc": 15, "at": "2026-10-22T13:10:00.000Z"}
FAR = {"lat": -38.960000, "lng": -68.059100, "acc": 20, "at": "2026-10-22T13:10:00.000Z"}


def _gps(base, **kw):
    return dict(base, **kw)


@tagged("post_install", "-at_install", "distri_reparto")
class TestRepartoGps(RepartoMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product_a.invoice_policy = "order"
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.seller = new_test_user(
            cls.env, login="vendedor_gps", groups="base.group_user,distri_pedido.grupo_vendedor",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)])
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param(PARAM_REQUIRED, "1")
        params.set_param(PARAM_ACCURACY, "100")
        params.set_param(PARAM_DISTANCE, "300")
        params.set_param(PARAM_RETENTION, "12")
        cls.partner = cls.partner_a
        cls.partner.write({"partner_latitude": HOME[0], "partner_longitude": HOME[1]})

    def _route_on_way(self):
        order = self._order(self.partner, self.product_a)
        route = self._route(order)
        self._depart(route, gps=_gps(NEAR, at="2026-10-22T12:00:00.000Z"))
        return route

    # ------------------------------------------------------------ app

    def test_app_blocks_without_position_and_carries_it_offline(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("Node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/gps_app.js"), root],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        # «Llegué» comes before the visit form
        self.assertTrue(out["gate"])
        # GPS required and denied: blocked, with help and «Reintentar»
        self.assertEqual(out["blocked"], {"queued": 0, "error": "denied", "retry": True,
                                          "help": True})
        # offline: the outbox event carries lat, lng, accuracy and the phone's time
        self.assertEqual(out["arrive"]["type"], "arrive")
        self.assertEqual(out["arrive"]["gps"], {"lat": -38.9516, "lng": -68.0591, "acc": 12,
                                                "at": "2026-10-22T15:00:00.000Z"})
        self.assertTrue(out["arrive"]["sheetClosed"])
        self.assertEqual(out["arrive"]["options"]["enableHighAccuracy"], True)
        self.assertLessEqual(out["arrive"]["options"]["maximumAge"], 60000)
        self.assertFalse(out["visit"]["online"])
        self.assertEqual(out["visit"]["gps"]["lat"], -38.9516)
        # not required: the action goes on and says why there is no position
        self.assertEqual(out["optional"], {"queued": 1, "gps": {"error": "unavailable"}})
        # the notice first, once, and its acceptance travels to the server
        self.assertTrue(out["notice"]["shown"])
        self.assertTrue(out["notice"]["text"])
        self.assertEqual(out["notice"]["queuedBefore"], 0)
        self.assertEqual(out["notice"]["types"], ["gps_notice", "arrive"])
        # outside an active route nothing is captured
        self.assertEqual(out["inactive"], {"calls": 0, "gps": True})

    def test_route_payload_says_what_the_app_needs(self):
        order = self._order(self.partner, self.product_a)
        route = self._route(order)
        gps = route._app_payload()["gps"]
        self.assertEqual(gps, {"required": True, "accuracy_m": 100, "retention_months": 12,
                               "notice_version": NOTICE_VERSION, "notice_accepted": False})
        res = self._sync([self._event(route, "gps_notice", {"version": NOTICE_VERSION})])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(self.driver.distri_gps_notice_version, NOTICE_VERSION)
        self.assertTrue(self.driver.distri_gps_notice_date)
        self.assertTrue(route._app_payload()["gps"]["notice_accepted"])

    # ------------------------------------------------------------ server

    def test_points_times_and_flags(self):
        route = self._route_on_way()
        stop = route.stop_ids
        res = self._sync([
            self._event(route, "arrive", {"stop_id": stop.id, "gps": FAR}),
            self._event(route, "visit", self._visit(
                stop, cash=1000, gps=_gps(NEAR, acc=250, at="2026-10-22T13:25:00.000Z"))),
        ])
        self.assertEqual([r["status"] for r in res], ["ok", "ok"], res)
        res = self._sync([self._event(route, "close", {"gps": _gps(
            NEAR, at="2026-10-22T14:00:00.000Z")})])
        self.assertEqual(res[0]["status"], "ok", res)
        points = route.gps_point_ids
        self.assertEqual(points.mapped("kind"), ["start", "arrive", "close_visit", "end"])
        start, arrive, close, end = points
        self.assertFalse(start.stop_id)
        self.assertEqual(arrive.stop_id, stop)
        self.assertTrue(arrive.far)
        self.assertGreater(arrive.distance_m, 900)
        self.assertIn("Lejos del cliente", arrive.flags)
        self.assertFalse(arrive.low_accuracy)
        self.assertTrue(close.low_accuracy)
        self.assertFalse(close.far)
        self.assertIn("Precisión baja (±250 m)", close.flags)
        self.assertEqual(close.device_time, datetime(2026, 10, 22, 13, 25))
        self.assertTrue(close.received_at)
        self.assertTrue(arrive.map_url.startswith("https://www.google.com/maps/search/?api=1&query="))
        # the event log never keeps the coordinates
        logged = self.env["distri.sync.event"].sudo().search([("route_id", "=", route.id)])
        self.assertTrue(logged)
        for event in logged:
            self.assertNotIn("-38.95", event.payload)
        # the customer's location is never overwritten by a visit
        self.assertEqual((self.partner.partner_latitude, self.partner.partner_longitude), HOME)

        data = self.env["distri.reparto.tiempos"].create({
            "date_from": route.date, "date_to": route.date})._distri_data()
        block = [b for b in data if b["route"] == route.name][0]
        self.assertEqual((block["start"], block["end"], block["total"]), ("09:00", "11:00",
                                                                           "2 h 00 min"))
        row = block["rows"][0]
        self.assertEqual(row["arrive"]["time"], "10:10")
        self.assertEqual(row["close"]["time"], "10:25")
        self.assertEqual(row["at_customer"], "15 min")
        self.assertEqual(row["gap"], "1 h 10 min")
        self.assertIn("Lejos del cliente", row["flags"])
        self.assertIn("Precisión baja", row["flags"])

    def test_without_position(self):
        route = self._route_on_way()
        stop = route.stop_ids
        # the GPS failed (not required on that phone): recorded and flagged
        self._sync([self._event(route, "arrive", {"stop_id": stop.id,
                                                  "gps": {"error": "denied"}})])
        point = route.gps_point_ids.filtered(lambda p: p.kind == "arrive")
        self.assertTrue(point.missing)
        self.assertEqual(point.missing_reason, "denied")
        self.assertIn("Ubicación no permitida", point.flags)
        self.assertFalse(point.map_url)
        # an edit of the reason afterwards is not a capture
        before = len(route.gps_point_ids)
        self._sync([self._event(route, "visit", self._visit(stop, outcome="not_delivered",
                                                            reason="Cerrado"))])
        self.assertEqual(len(route.gps_point_ids), before)

    def test_arrive_needs_an_active_route(self):
        order = self._order(self.partner, self.product_a)
        route = self._route(order)
        res = self._sync([self._event(route, "arrive", {"stop_id": route.stop_ids.id,
                                                        "gps": NEAR})])
        self.assertEqual(res[0]["status"], "error")
        self.assertFalse(route.gps_point_ids)

    def test_override_is_for_administracion_only(self):
        order = self._order(self.partner, self.product_a)
        route = self._route(order)
        for user in (self.driver, self.seller):
            with self.assertRaises(AccessError):
                route.with_user(user).action_gps_override()
            with self.assertRaises(AccessError):
                self.env["distri.route.gps.override"].with_user(user).create(
                    {"route_id": route.id, "reason": "GPS roto"}).action_confirm()
        action = route.action_gps_override()
        wizard = self.env[action["res_model"]].with_context(action["context"]).create(
            {"reason": "El GPS del celular está roto"})
        wizard.action_confirm()
        self.assertTrue(route.gps_override)
        self.assertEqual(route.gps_override_user_id, self.env.user)
        self.assertFalse(route._app_payload()["gps"]["required"])
        data = self.env["distri.reparto.tiempos"].create({
            "date_from": route.date, "date_to": route.date})._distri_data()
        block = [b for b in data if b["route"] == route.name][0]
        self.assertTrue(block["override"])
        self.assertEqual(block["override_reason"], "El GPS del celular está roto")

    def test_report_is_for_administracion_and_gerencia_only(self):
        route = self._route_on_way()
        for user in (self.driver, self.seller):
            with self.assertRaises(AccessError):
                self.env["distri.reparto.tiempos"].with_user(user).create({})
            with self.assertRaises(AccessError):
                self.env["report.distri_pedido.tiempos_reparto_documento"].with_user(
                    user)._get_report_values([])
            with self.assertRaises(AccessError):
                route.gps_point_ids.with_user(user).read(["latitude"])
        manager = new_test_user(
            self.env, login="gerencia_gps", groups="base.group_user,distri_pedido.grupo_gerencia",
            company_id=self.env.company.id, company_ids=[Command.set(self.env.company.ids)])
        wizard = self.env["distri.reparto.tiempos"].with_user(manager).create({
            "date_from": route.date, "date_to": route.date, "driver_id": self.driver.id})
        html = self.env["ir.actions.report"].with_user(manager)._render_qweb_html(
            "distri_pedido.report_tiempos_reparto_html", wizard.ids)[0].decode()
        self.assertIn("Tiempos de reparto", html)
        self.assertIn(self.driver.name, html)
        self.assertIn("https://www.google.com/maps/search/?api=1&amp;query=", html)
        self.assertNotIn("<iframe", html)
        pdf = self.env["ir.actions.report"].with_user(manager).with_context(
            force_report_rendering=True)._render_qweb_pdf(
            "distri_pedido.report_tiempos_reparto", wizard.ids)[0]
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_save_as_customer_location_by_hand(self):
        route = self._route_on_way()
        stop = route.stop_ids
        self.partner.write({"partner_latitude": 0.0, "partner_longitude": 0.0})
        self._sync([
            self._event(route, "arrive", {"stop_id": stop.id, "gps": _gps(NEAR, acc=400)}),
            self._event(route, "visit", self._visit(stop, cash=1000, gps=NEAR)),
        ])
        bad = route.gps_point_ids.filtered(lambda p: p.kind == "arrive")
        good = route.gps_point_ids.filtered(lambda p: p.kind == "close_visit")
        # no saved location: no distance, no flag, nothing written by itself
        self.assertFalse(good.far)
        self.assertEqual(self.partner.partner_latitude, 0.0)
        self.assertFalse(bad.can_save_location)
        self.assertTrue(good.can_save_location)
        with self.assertRaises(AccessError):
            good.with_user(self.driver).action_save_customer_location()
        good.action_save_customer_location()
        self.assertAlmostEqual(self.partner.partner_latitude, NEAR["lat"], places=6)
        self.assertAlmostEqual(self.partner.partner_longitude, NEAR["lng"], places=6)
        self.assertEqual(self.partner.distri_location_user_id, self.env.user)

    def test_purge_keeps_the_times(self):
        route = self._route_on_way()
        old, new = route.gps_point_ids, None
        old.write({"device_time": fields.Datetime.now() - timedelta(days=400)})
        self._sync([self._event(route, "arrive", {"stop_id": route.stop_ids.id, "gps": NEAR})])
        new = route.gps_point_ids - old
        when = old.device_time
        purged = self.env["distri.route.gps"]._cron_purge()
        self.assertGreaterEqual(purged, 1)
        self.assertTrue(old.purged)
        self.assertEqual((old.latitude, old.longitude, old.accuracy), (0.0, 0.0, 0.0))
        self.assertEqual(old.device_time, when)
        self.assertFalse(old.map_url)
        self.assertFalse(new.purged)
        self.assertEqual(new.latitude, NEAR["lat"])
        cron = self.env.ref("distri_pedido.cron_gps_purge")
        self.assertTrue(cron.active)

    def test_settings_for_administracion_only(self):
        with self.assertRaises(AccessError):
            self.env["distri.gps.config"].with_user(self.driver).create({}).action_save()
        config = self.env["distri.gps.config"].create({})
        self.assertTrue(config.gps_required)
        self.assertEqual((config.accuracy_m, config.distance_m, config.retention_months),
                         (100, 300, 12))
        config.write({"gps_required": False, "distance_m": 500, "retention_months": 6})
        config.action_save()
        params = self.env["ir.config_parameter"].sudo()
        self.assertEqual(params.get_param(PARAM_REQUIRED), "0")
        self.assertEqual(params.get_param(PARAM_DISTANCE), "500")
        self.assertEqual(params.get_param(PARAM_RETENTION), "6")
