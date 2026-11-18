"""19.0.1.110.0: «Planificación de rutas» (/rutas, models/planificacion.py),
«Hoja de ruta y pagos» and «Remitos» (models/hoja_ruta.py,
views/hoja_ruta.xml).

The planning board moves orders through the route's own methods; these
tests pin when a move is allowed (until «Controlado» / «Salir a repartir»,
never an order already ticked on the truck), what the driver's phone sees
(models/reparto_sync.py) and what the printed documents carry and never
carry (the receipt code, «factura» as a title, signatures, a time in
«Impresa el»)."""
import re
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.hoja_ruta import DEFAULT_HOLDER, PARAM_HOLDER
from odoo.addons.distri_pedido.models.remito import numero_de_remito
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

FETCH = {"X-Distri-Fetch": "1"}
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')


class PlanningMixin(RepartoMixin):

    @classmethod
    def _setup_planning(cls):
        cls._setup_reparto()
        company = cls.env.company
        # the copy of production has routes of today: out of the way (to a far
        # future day), so the board of today holds only the test's routes
        far = fields.Date.add(today_ar(), days=400)
        cls.env["distri.route"].sudo().search([
            ("kind", "=", "route"), "|", ("state", "=", "draft"),
            ("date", ">=", fields.Date.subtract(today_ar(), days=2))]).write({"date": far})

        def user(login, name, groups):
            return new_test_user(cls.env, login=login, name=name, groups=groups,
                                 company_id=company.id, company_ids=[Command.set(company.ids)])

        cls.admin = user("denis_plan", "Denis Plan",
                         "base.group_user,distri_pedido.grupo_administracion")
        cls.manager = user("gonzalo_plan", "Gonzalo Plan",
                           "base.group_user,distri_pedido.grupo_gerencia")
        cls.seller = user("vendedor_plan", "Vendedor Plan",
                          "base.group_user,distri_pedido.grupo_vendedor")
        cls.packer = user("armador_plan", "Armador Plan",
                          "base.group_user,distri_pedido.grupo_armador")
        Zone = cls.env["distri.zona"]
        cls.zone_a = Zone.create({"name": "Zona Plan Norte"})
        cls.zone_b = Zone.create({"name": "Zona Plan Sur"})
        cls.product = cls.env["product.product"].create({
            "name": "Producto plan", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.orders = cls.env["sale.order"]
        for name, zone in (("DESPENSA PLAN UNO", cls.zone_a), ("KIOSCO PLAN DOS", cls.zone_a),
                           ("ALMACEN PLAN TRES", cls.zone_b)):
            partner = cls.env["res.partner"].create({
                "name": name, "street": "Calle Plan 1", "city": zone.name})
            cls.orders |= cls._order(partner, cls.product, qty=2, price=100)

    def _planner(self, user=None):
        return self.env["distri.route"].with_user(user or self.admin)

    def _new_route(self, driver=None, trip="1", user=None):
        return self._planner(user).planning_create(today_ar(), trip, driver)

    def _move(self, order, route=None, before=None, user=None):
        Route = self._planner(user)
        return Route.planning_move(order.with_user(user or self.admin),
                                   route.with_user(user or self.admin) if route else None,
                                   before)


@tagged("post_install", "-at_install", "distri_v110", "distri_v110_planificacion")
class TestPlanificacion(PlanningMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_planning()

    def setUp(self):
        super().setUp()
        self.o1, self.o2, self.o3 = self.orders
        self.r1 = self._new_route(self.driver)
        self.r2 = self._new_route(self.other_driver)

    # ------------------------------------------------------------ access

    def _csrf(self, path="/rutas"):
        return CSRF_RE.search(self.url_open(path).text).group(1)

    def _post(self, action, data):
        data = dict(data, csrf_token=self._csrf(), fecha=fields.Date.to_string(today_ar()))
        return self.url_open("/rutas/accion/%s" % action, data=data, headers=FETCH)

    def test_access_per_group(self):
        for user in (self.driver, self.seller, self.packer):
            self.authenticate(user.login, user.login)
            self.assertEqual(self.url_open("/rutas").status_code, 403, user.login)
            self.assertEqual(self.url_open("/rutas/tablero").status_code, 403, user.login)
            res = self.url_open("/rutas/accion/nueva", data={"csrf_token": "x"}, headers=FETCH)
            self.assertIn(res.status_code, (400, 403), user.login)
            with self.assertRaises(UserError):
                self._planner(user).planning_create(today_ar(), "1")
        for user in (self.admin, self.manager):
            self.authenticate(user.login, user.login)
            page = self.url_open("/rutas")
            self.assertEqual(page.status_code, 200, user.login)
            self.assertIn("rutas.js", page.text)
            self.assertIn("Sin ruta · depósito", page.text)
            self.assertIn("Asignar pedidos armados de las zonas", page.text,
                          "kept for the sub-zones, as a secondary action")
            data = self.url_open("/rutas/tablero").json()
            self.assertTrue(data["ok"])
            self.assertIn(self.o1.name, data["html"])

    def test_board_lists_free_orders_by_zone_and_held_locked(self):
        self._move(self.o1, self.r1)
        self.o3.with_user(self.manager).distri_retener("Debe plata")
        board = self._planner()._planning_board()
        cards = {c["name"]: c for g in board["groups"] for c in g["orders"]}
        self.assertNotIn(self.o1.name, cards, "on a route: not in the depot column")
        self.assertFalse(cards[self.o2.name]["held"])
        self.assertTrue(cards[self.o3.name]["held"])
        self.assertEqual(cards[self.o3.name]["held_reason"], "Debe plata")
        zones = [g["name"] for g in board["groups"]]
        self.assertIn(self.zone_a.name, zones)
        self.assertIn(self.zone_b.name, zones)
        columns = {c["id"]: c for c in board["columns"]}
        self.assertEqual(columns[self.r1.id]["title"], "Ruta 1 · Viaje 1")
        self.assertEqual(columns[self.r2.id]["title"], "Ruta 2 · Viaje 1")
        self.assertEqual([t["id"] for t in board["targets"]], [self.r1.id, self.r2.id])
        with self.assertRaisesRegex(UserError, "retener|retenido"):
            self._move(self.o3, self.r2)

    # ------------------------------------------------------ routes, driver

    def test_new_route_empty_waits_for_its_driver(self):
        route = self._new_route()
        self.assertFalse(route.driver_id)
        self.assertEqual((route.kind, route.state, route.date), ("route", "draft", today_ar()))
        self.assertIn("Sin repartidor", route.name)
        self.assertEqual(route._planning_title(), "Ruta 3 · Viaje 1")
        with self.assertRaisesRegex(UserError, "repartidor"):
            route.with_user(self.admin).action_mark_controlled()
        route.with_user(self.admin).planning_set_driver(self.driver)
        self.assertEqual(route.driver_id, self.driver)
        # the same driver, day and trip twice is refused; trip 2 is fine
        with self.assertRaisesRegex(UserError, "ya tiene el viaje 1"):
            self._new_route(self.driver)
        self.assertEqual(self._new_route(self.driver, trip="2")._planning_title(), "Ruta 1 · Viaje 2")
        with self.assertRaisesRegex(UserError, "de hoy en adelante"):
            self._planner().planning_create(fields.Date.subtract(today_ar(), days=1), "1")
        with self.assertRaisesRegex(UserError, "permiso de reparto"):
            route.with_user(self.admin).planning_set_driver(self.seller)
        # an empty route created by mistake is deleted
        empty = self._new_route()
        empty.with_user(self.admin).planning_delete()
        self.assertFalse(empty.exists())

    def test_driver_fixed_once_he_started_loading(self):
        self._move(self.o1, self.r1)
        self.r1.with_user(self.admin).planning_set_driver(self.other_driver)
        self.r1.with_user(self.admin).planning_set_driver(self.driver)
        self._sync([self._event(self.r1, "load", {"stop_id": self.r1.stop_ids.id, "loaded": True})])
        with self.assertRaisesRegex(UserError, "empezó a cargar"):
            self.r1.with_user(self.admin).planning_set_driver(self.other_driver)

    # ------------------------------------------------------------- moves

    def test_move_in_between_back_and_reorder(self):
        stop = self._move(self.o1, self.r1)
        self.assertEqual(stop.route_id, self.r1)
        self.assertIn(self.zone_a, self.r1.zone_ids, "the route takes the order's zone")
        self.assertEqual(stop.order_amount, self.o1.amount_total, "the route's own snapshot")
        # to the other route: one stop only, the old one is gone
        new = self._move(self.o1, self.r2)
        self.assertFalse(self.r1.stop_ids)
        self.assertEqual(self.o1.distri_stop_ids, new)
        self.assertIn("pasa a la Ruta 2", self.r1.message_ids[:1].body)
        # back to the depot column
        self.assertFalse(self._move(self.o1, None))
        self.assertFalse(self.o1.distri_stop_ids)
        self.assertIn(self.o1, self._planner()._planning_depot_orders()[0])
        # order inside a route: in front of another stop, then by ids
        a = self._move(self.o1, self.r1)
        b = self._move(self.o2, self.r1)
        c = self._move(self.o3, self.r1, before=a)
        self.assertEqual(self.r1.stop_ids.ids, [c.id, a.id, b.id])
        self._move(self.o2, self.r1, before=c)
        self.assertEqual(self.r1.stop_ids.ids, [b.id, c.id, a.id])
        self.r1.with_user(self.admin).planning_reorder([a.id, b.id, c.id])
        self.assertEqual(self.r1.stop_ids.ids, [a.id, b.id, c.id])
        self.assertEqual(self.r1.stop_ids.mapped("sequence"), [10, 20, 30])

    def test_no_move_once_controlled_or_departed(self):
        stop = self._move(self.o1, self.r1)
        self._move(self.o2, self.r1)
        # «Controlado» at the depot: fixed, also for new orders
        self._sync([self._event(self.r1, "load", {"stop_id": s.id, "loaded": True})
                    for s in self.r1.stop_ids])
        self.r1.stop_ids.with_user(self.admin).action_dispatch_check()
        self.r1.with_user(self.admin).action_mark_controlled()
        with self.assertRaisesRegex(UserError, "ya se controló"):
            self._move(self.o3, self.r1)
        # «Salir a repartir»: the driver's phone owns the stops
        self._sync([self._event(self.r1, "depart", {})])
        self.assertEqual(self.r1.state, "on_route")
        with self.assertRaisesRegex(UserError, "ya salió"):
            self._move(self.o1, self.r2)
        with self.assertRaisesRegex(UserError, "ya salió"):
            self._move(self.o1, None)
        with self.assertRaisesRegex(UserError, "ya salió"):
            self.r1.with_user(self.admin).planning_reorder(self.r1.stop_ids.ids[::-1])
        with self.assertRaisesRegex(UserError, "ya salió"):
            self.r1.with_user(self.admin).planning_set_driver(self.other_driver)
        self.assertEqual(stop.route_id, self.r1)
        # the departed column offers the next trip, which the board creates
        column = {c["id"]: c for c in self._planner()._planning_board()["columns"]}[self.r1.id]
        self.assertFalse(column["editable"])
        self.assertEqual(column["next_trip"], "2")
        self.assertEqual(column["locked"], "Ya salió: los cambios de paradas los hace el repartidor.")
        trip2 = self._new_route(self.driver, trip="2")
        self._move(self.o3, trip2)
        self.assertEqual(self.o3.distri_stop_ids.route_id, trip2)

    def test_order_ticked_on_the_truck_does_not_move(self):
        stop = self._move(self.o1, self.r1)
        self._sync([self._event(self.r1, "load", {"stop_id": stop.id, "loaded": True})])
        with self.assertRaisesRegex(UserError, "arriba de la camioneta"):
            self._move(self.o1, self.r2)
        # unticked again on the phone: free to move
        self._sync([self._event(self.r1, "load", {"stop_id": stop.id, "loaded": False})])
        self._move(self.o1, self.r2)
        # ticked by the depot (/despacho): the same
        stop = self.o1.distri_stop_ids
        stop.with_user(self.admin).action_dispatch_check()
        with self.assertRaisesRegex(UserError, "arriba de la camioneta"):
            self._move(self.o1, None)

    def test_one_active_route_and_left_orders(self):
        """A stop of the order that came back unloaded does not count; an
        order the dispatcher left at the depot of a route goes back on it
        and its «Quedan en el depósito» row goes away."""
        stop = self._move(self.o1, self.r1)
        stop.with_user(self.admin).action_leave_at_depot("No entra en la camioneta")
        self.assertTrue(self.r1.left_order_ids)
        self._move(self.o1, self.r1)
        self.assertFalse(self.r1.left_order_ids)
        with self.assertRaises(ValidationError):
            self.env["distri.route.stop"].create({"route_id": self.r2.id, "order_id": self.o1.id})

    # ------------------------------------------------------ the driver app

    def test_driver_app_sees_the_change_and_departure_waits(self):
        """The sync behaviour chosen: moves before «Controlado» are free; the
        phone polls /reparto/api/load and downloads again when the set of
        stops changes, and the server refuses «Salir a repartir» until the
        added order is ticked by both."""
        first = self._move(self.o1, self.r1)
        self._sync([self._event(self.r1, "load", {"stop_id": first.id, "loaded": True})])
        first.with_user(self.admin).action_dispatch_check()
        self.authenticate(self.driver.login, self.driver.login)
        before = self.url_open("/reparto/api/load?id=%s" % self.r1.id).json()["route"]
        self.assertEqual([s["id"] for s in before["stops"]], [first.id])
        added = self._move(self.o2, self.r1)
        after = self.url_open("/reparto/api/load?id=%s" % self.r1.id).json()["route"]
        self.assertEqual(sorted(s["id"] for s in after["stops"]), sorted([first.id, added.id]),
                         "a different set of stops: the phone downloads the route again")
        full = self.url_open("/reparto/api/route?id=%s" % self.r1.id).json()["route"]
        self.assertIn(self.o2.name, [s["order"] for s in full["stops"]])
        res = self._sync([self._event(self.r1, "depart", {})])
        self.assertEqual(res[0]["status"], "error")
        self.assertIn("doble control", res[0]["error"])
        # moved away before departure: it leaves the phone, the route can go
        self._move(self.o2, self.r2)
        after = self.url_open("/reparto/api/load?id=%s" % self.r1.id).json()["route"]
        self.assertEqual([s["id"] for s in after["stops"]], [first.id])
        res = self._sync([self._event(self.r1, "depart", {})])
        self.assertEqual(res[0]["status"], "ok", res)
        # the other driver's route got it
        self.assertEqual(self.o2.distri_stop_ids.route_id, self.r2)

    # -------------------------------------------------------------- HTTP

    def test_http_actions_answer_with_the_board(self):
        self.authenticate(self.admin.login, self.admin.login)
        res = self._post("mover", {"order_id": self.o1.id, "route_id": self.r1.id}).json()
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.o1.distri_stop_ids.route_id, self.r1)
        self.assertIn('data-route="%s"' % self.r1.id, res["html"])
        self._post("mover", {"order_id": self.o2.id, "route_id": self.r1.id})
        ids = self.r1.stop_ids.ids
        res = self._post("orden", {"route_id": self.r1.id,
                                   "stop_ids": ",".join(map(str, ids[::-1]))}).json()
        self.assertTrue(res["ok"])
        self.assertEqual(self.r1.stop_ids.ids, ids[::-1])
        res = self._post("nueva", {"trip": "2", "driver_id": ""}).json()
        self.assertTrue(res["ok"])
        self.assertIn("Ruta 1 · Viaje 2", res["message"])
        new = self.env["distri.route"].search([("trip", "=", "2"), ("driver_id", "=", False),
                                               ("date", "=", today_ar())])
        res = self._post("repartidor", {"route_id": new.id, "driver_id": self.driver.id}).json()
        self.assertTrue(res["ok"])
        self.assertEqual(new.driver_id, self.driver)
        res = self._post("zonas", {"route_id": new.id, "zone_ids": str(self.zone_b.id)}).json()
        self.assertEqual(new.zone_ids, self.zone_b)
        res = self._post("asignar", {"route_id": new.id}).json()
        self.assertTrue(res["ok"], res)
        self.assertEqual(new.stop_ids.order_id, self.o3)
        # a refusal: 400 with the reason
        self._sync([self._event(self.r1, "load", {"stop_id": self.o1.distri_stop_ids.id,
                                                   "loaded": True})])
        res = self._post("mover", {"order_id": self.o1.id, "route_id": "0"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("arriba de la camioneta", res.json()["error"])
        self.assertEqual(self.o1.distri_stop_ids.route_id, self.r1)


@tagged("post_install", "-at_install", "distri_v110", "distri_v110_planillas")
class TestPlanillas(PlanningMixin, HttpCase):
    """«Hoja de ruta y pagos» and «Remitos» (QWeb HTML of the PDF reports)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_planning()
        cls.env["ir.config_parameter"].sudo().set_param("distri_pedido.alias_transferencia", "sepiko")
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_HOLDER, DEFAULT_HOLDER)
        # two orders packed the real way: their remito (R-…) is validated
        pricelist = cls.env["product.pricelist"].create({
            "name": "Planillas", "currency_id": cls.env.company.currency_id.id})
        cls.packed = cls.env["sale.order"]
        for name, qty in (("KIOSCO REMITO PLAN", 4), ("DESPENSA REMITO PLAN", 3)):
            partner = cls.env["res.partner"].create({
                "name": name, "street": "Belgrano 120", "city": cls.zone_a.name})
            order = cls.env["sale.order"].create({
                "partner_id": partner.id, "pricelist_id": pricelist.id,
                "distri_comentario": "Recibimos de 9 a 13 h",
                "order_line": [Command.create({"product_id": cls.product.id, "product_uom_qty": qty,
                                               "price_unit": 250.0, "tax_ids": [Command.clear()]})]})
            order.action_confirm()
            work = order.with_user(cls.packer).sudo()
            assert work.distri_tomar() == ""
            assert work.distri_terminar(2) == ""
            order.sudo().write({"distri_receipt_code": "7319"})
            cls.packed |= order

    def setUp(self):
        super().setUp()
        self.route = self._new_route(self.driver)
        for order in self.packed:
            self._move(order, self.route)

    def _html(self, report, records):
        return self.env["ir.actions.report"]._render_qweb_html(report, records.ids)[0].decode()

    def _text(self, html):
        return " ".join(re.sub(r"<[^>]+>", " ", html).split())

    def test_route_sheet(self):
        first = self.route.stop_ids[0]
        with patch.object(type(self.env["sale.order"]), "_distri_previous_debt",
                          lambda order: 50000.0 if order == first.order_id else 0.0):
            html = self._html("distri_pedido.report_hoja_ruta", self.route)
        text = self._text(html)
        self.assertIn("HOJA DE RUTA", text)
        self.assertIn("Ruta 1 · Viaje 1", text)
        printed = re.search(r"Impresa el (\S+)( \S+)?", text)
        self.assertEqual(printed.group(1), today_ar().strftime("%d/%m/%Y"))
        self.assertFalse(re.search(r"Impresa el \S+ (a las )?\d{1,2}:\d{2}", text), "no time")
        for label in ("Repartidor y cobrador", "Fecha", "Zonas", "Bultos", "Despachó"):
            self.assertIn(label, text)
        for absent in ("Salió", "a las", "Pedidos del viaje"):
            self.assertNotIn(absent, text)
        self.assertIn("PARA TRANSFERIR · ALIAS", text)
        self.assertIn("sepiko", text)
        self.assertIn("Titular: Gonzalo Granero · Distri Neuquén", text)
        # blank columns, fixed rows, boxes; never the code nor signatures
        for label in ("Código", "Hora", "Pagado", "Medio", "Gastos del cobrador",
                      "Faltante o sobrante", "Flete", "Otros cobros", "Total de la ruta",
                      "E = efectivo · T = transferencia · AC = a cuenta (no pagó) · D = devolución",
                      "Efectivo entregado en caja"):
            self.assertIn(label, text)
        self.assertNotIn("7319", text, "the receipt code is never printed")
        self.assertNotRegex(text.lower(), r"firma|aclaraci[oó]n")
        self.assertGreaterEqual(html.count('class="dr-blank"'), 3)
        # each stop in order with what to collect and the debt breakdown
        for order in self.packed:
            self.assertIn(order.name, text)
            self.assertIn(order._distri_remitos_texto(), text)
        self.assertIn("$ 51.000", text, "remito 1.000 + previous debt 50.000")
        self.assertIn("remito $ 1.000 + debía $ 50.000", text)
        self.assertIn("Recibimos de 9 a 13 h", text)
        self.assertIn("$ 51.750", text, "the route total")

    def test_remitos_match_the_picking(self):
        html = self._html("distri_pedido.report_remitos_ruta", self.route)
        text = self._text(html)
        self.assertEqual(text.count("REMITO"), 2)
        self.assertEqual(html.count("✂ cortar"), 1, "one cut line between two remitos")
        self.assertIn("page-break-inside:avoid", html)
        for order in self.packed:
            picking = order._distri_remitos()
            self.assertTrue(picking)
            number = numero_de_remito(picking)
            self.assertTrue(number.startswith("R-"), number)
            self.assertIn(number, text)
            self.assertIn("Pedido %s" % order.name, text)
            move = picking.move_ids
            self.assertIn("%g Producto plan $ 250 $ %s" % (
                move.quantity, "{:,.0f}".format(250 * move.quantity).replace(",", ".")), text)
        self.assertIn("Reparto · %s · Ruta 1 · Viaje 1 · Repartidor Test · 2 bultos" % self.zone_a.name,
                      text)
        self.assertIn("total a pagar $ 1.000", text)
        self.assertIn("sepiko", text)
        self.assertIn("Mandá el comprobante de la transferencia desde la página de tu pedido. "
                      "Documento no válido como factura.", text)
        # «factura» only in that legal line: never the title
        self.assertEqual(text.lower().count("factura"), 2)
        self.assertNotIn("FACTURA", text)
        self.assertNotIn("7319", text)
        self.assertNotRegex(text.lower(), r"firma|aclaraci[oó]n|salió")

    def test_remito_of_one_order_and_buttons(self):
        order = self.packed[0]
        text = self._text(self._html("distri_pedido.report_remito_pedido", order))
        self.assertIn(numero_de_remito(order._distri_remitos()), text)
        self.assertIn("Ruta 1 · Viaje 1", text)
        self.assertEqual(self.route.action_print_route_sheet()["report_name"],
                         "distri_pedido.hoja_ruta_documento")
        self.assertEqual(self.route.action_print_remitos()["report_name"],
                         "distri_pedido.remitos_ruta_documento")
        self.assertEqual(order.action_distri_print_remito()["report_name"],
                         "distri_pedido.remito_pedido_documento")
        form = self.env["distri.route"].get_view(view_type="form")["arch"]
        self.assertIn("action_print_route_sheet", form)
        self.assertIn("action_print_remitos", form)
        # the board links both documents of every route
        self.authenticate(self.admin.login, self.admin.login)
        page = self.url_open("/rutas").text
        self.assertIn("/report/pdf/distri_pedido.hoja_ruta_documento/%s" % self.route.id, page)
        self.assertIn("/report/pdf/distri_pedido.remitos_ruta_documento/%s" % self.route.id, page)
        pdf = self.url_open("/report/pdf/distri_pedido.hoja_ruta_documento/%s" % self.route.id)
        self.assertEqual(pdf.status_code, 200)

    def test_holder_parameter_and_default(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_HOLDER, "Otro Titular")
        text = self._text(self._html("distri_pedido.report_hoja_ruta", self.route))
        self.assertIn("Titular: Otro Titular · Distri Neuquén", text)
        self.env["ir.config_parameter"].sudo().set_param(PARAM_HOLDER, "")
        text = self._text(self._html("distri_pedido.report_hoja_ruta", self.route))
        self.assertIn("Titular: Gonzalo Granero · Distri Neuquén", text)


@tagged("post_install", "-at_install", "distri_v110")
class TestNoApproveDriverExpenses(HttpCase):
    """C (owner): driver expenses have no «Aprobar» anywhere."""

    def test_no_approve(self):
        Expense = self.env["distri.route.expense"]
        self.assertFalse(hasattr(Expense, "action_approve"))
        views = self.env["ir.ui.view"].search([("model", "=", "distri.route.expense")])
        self.assertTrue(views)
        for view in views:
            arch = view.arch_db or ""
            self.assertNotIn("Aprobar", arch, view.xml_id)
            self.assertNotIn("action_approve", arch, view.xml_id)
        for path in ("rutas/rutas.js", "reparto/app.js"):
            from odoo.tools.misc import file_open
            with file_open("distri_pedido/static/src/%s" % path) as handle:
                self.assertNotIn("Aprobar", handle.read(), path)
