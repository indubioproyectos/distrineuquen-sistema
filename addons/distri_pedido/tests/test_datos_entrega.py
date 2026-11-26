import re
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.models import datos_entrega
from odoo.addons.distri_pedido.models.reparto_cliente import WEEKDAYS
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@tagged("post_install", "-at_install", "distri_reparto", "distri_datos_entrega")
class TestDatosEntrega(RepartoMixin, HttpCase):
    """«Tu pedido» with the route status and «Datos para la entrega»
    (models/datos_entrega.py). Nothing is ever sent to the customer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls.seller = new_test_user(
            cls.env, login="vendedor_entrega", name="Vendedor Entrega",
            groups="base.group_user,sales_team.group_sale_salesman",
        )
        cls.partner = cls.env["res.partner"].create({
            "name": "DESPENSA DE PRACTICA", "street": "Calle Falsa 123", "city": "Neuquén",
            "email": "despensa@example.com", "phone": "299 555-0101",
            "partner_latitude": -38.95, "partner_longitude": -68.06,
            "distri_location_user_id": cls.driver.id,
            "distri_location_date": fields.Datetime.now(),
        })
        cls.product = cls.env["product.product"].create({
            "name": "Producto entrega", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.order = cls._order(cls.partner, cls.product, qty=3, price=100)
        cls.order.user_id = cls.seller
        cls.order._portal_ensure_token()
        cls.token = cls.order.access_token

    # ------------------------------------------------------------ helpers

    def _page(self):
        return self.url_open("/pedido/%s" % self.token).text

    def _entrega_url(self, order=None):
        return "/pedido/%s/entrega" % (order or self.order).access_token

    def _post(self, data, order=None):
        page = self.url_open(self._entrega_url(order))
        self.assertEqual(page.status_code, 200)
        csrf = CSRF_RE.search(page.text).group(1)
        return self.url_open(self._entrega_url(order), data=dict(data, csrf_token=csrf))

    def _outgoing(self):
        """Everything that could reach the customer."""
        partner = self.partner
        return (
            self.env["mail.mail"].sudo().search_count([("recipient_ids", "in", partner.ids)]),
            self.env["mail.notification"].sudo().search_count([("res_partner_id", "=", partner.id)]),
            self.env["distri.whatsapp.message"].sudo().search_count([("direction", "=", "out")]),
        )

    # ------------------------------------------------------- route status

    def test_status_assigned_on_route_and_not_delivered(self):
        self.assertIn("Tu pedido está armado", self._page())
        route = self._route(self.order)
        day = "%s %s" % (WEEKDAYS[route.date.weekday()], route.date.strftime("%d/%m"))
        self.assertIn("Tu pedido está en la ruta del %s" % day, self._page())

        self._depart(route)
        self.assertEqual(route.state, "on_route")
        page = self._page()
        self.assertIn("Tu pedido salió a repartir", page)
        self.assertNotIn("en la ruta del", page)

        stop = route.stop_ids
        res = self._sync([self._event(route, "visit", self._visit(
            stop, outcome="not_delivered", reason="El dueño estaba de mal humor"))])
        self.assertEqual(res[0]["status"], "ok", res)
        page = self._page()
        self.assertIn("No pudimos entregarlo", page)
        self.assertIn("Vamos a coordinar una nueva entrega", page)
        # the driver's internal reason never reaches the customer
        self.assertNotIn("mal humor", page)
        self.assertNotIn("salió a repartir", page)

        # put on another route: the newest stop decides
        stop.write({"unloaded": True})
        other = self.env["distri.route"].sudo().create({
            "driver_id": self.driver.id, "date": fields.Date.add(today_ar(), days=1)})
        stop.reassign_route_id = other
        stop.action_reassign()
        day = "%s %s" % (WEEKDAYS[other.date.weekday()], other.date.strftime("%d/%m"))
        self.assertIn("Tu pedido está en la ruta del %s" % day, self._page())

    def test_status_delivered_wins(self):
        route = self._route(self.order)
        self._depart(route)
        self._sync([self._event(route, "visit", self._visit(route.stop_ids))])
        page = self._page()
        self.assertIn("Tu pedido fue entregado", page)
        self.assertNotIn("salió a repartir", page)

    # ------------------------------------------------------------- access

    def test_access_rules(self):
        page = self._page()
        self.assertIn(self._entrega_url(), page)
        res = self.url_open(self._entrega_url())
        self.assertEqual(res.status_code, 200)
        self.assertIn("Datos para la entrega", res.text)
        self.assertIn("Estoy en mi negocio, usar mi ubicación", res.text)

        res = self.url_open("/pedido/no-existe-este-token/entrega")
        self.assertTrue(res.url.endswith("/pedido"), res.url)

        generic = self.env["res.partner"].sudo()._distri_generic_partner_for(False)
        web = self._order(generic, self.product)
        web._portal_ensure_token()
        self.assertNotIn(self._entrega_url(web), self.url_open("/pedido/%s" % web.access_token).text)
        res = self.url_open(self._entrega_url(web))
        self.assertTrue(res.url.endswith("/pedido/%s" % web.access_token), res.url)

        cancelled = self._order(self.partner, self.product)
        cancelled._portal_ensure_token()
        cancelled._action_cancel()
        res = self.url_open(self._entrega_url(cancelled))
        self.assertTrue(res.url.endswith("/pedido/%s" % cancelled.access_token), res.url)

    def test_post_needs_csrf(self):
        res = self.url_open(self._entrega_url(), data={"accion": "aviso", "tipo": "other_day"})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.order.distri_customer_notice_kind)

    # ------------------------------------------------------ days and hours

    def test_hours_update_the_driver_fields(self):
        res = self._post({"accion": "horarios", "dia_0": "1", "dia_2": "1", "dia_4": "1",
                          "desde_0": "08:00", "hasta_0": "12:30",
                          "desde_1": "17:00", "hasta_1": "20:30"})
        self.assertIn("Listo, guardamos tus días y horarios", res.text)
        self.assertEqual(self.partner.distri_delivery_days, "0,2,4")
        self.assertEqual(self.partner.distri_delivery_hours, "08:00-12:30;17:00-20:30")
        self.assertTrue(self.partner.distri_customer_hours_date)

        route = self._route(self.order)
        customer = route._app_payload()["stops"][0]["customer"]
        self.assertEqual(customer["days"], "0,2,4")
        self.assertEqual(customer["hours"], "08:00-12:30;17:00-20:30")
        self.assertTrue(customer["hours_by_customer_date"])

    def test_hours_are_validated(self):
        self.partner.write({"distri_delivery_days": "1", "distri_delivery_hours": "09:00-13:00"})
        cases = [
            ({"dia_0": "1", "desde_0": "12:00", "hasta_0": "08:00"}, "terminar después de empezar"),
            ({"desde_0": "08:00", "hasta_0": "12:00"}, "Marcá al menos un día"),
            ({"dia_0": "1"}, "Poné al menos un horario"),
            ({"dia_0": "1", "desde_0": "08:00"}, "Completá el horario"),
            ({"dia_0": "1", "desde_0": "08:00", "hasta_0": "13:00",
              "desde_1": "12:00", "hasta_1": "18:00"}, "se superponen"),
            ({"dia_0": "1", "desde_0": "8h", "hasta_0": "12:00"}, "Revisá el horario"),
        ]
        for data, error in cases:
            res = self._post(dict(data, accion="horarios"))
            self.assertIn(error, res.text)
        self.assertEqual(self.partner.distri_delivery_days, "1")
        self.assertEqual(self.partner.distri_delivery_hours, "09:00-13:00")

    # ---------------------------------------------------------------- GPS

    def test_location_never_overwrites_the_driver(self):
        res = self._post({"accion": "ubicacion", "lat": "-38.9512345", "lng": "-68.0598765",
                          "precision": "18.4"})
        self.assertIn("Listo, recibimos tu ubicación", res.text)
        self.assertAlmostEqual(self.partner.partner_latitude, -38.95)
        self.assertAlmostEqual(self.partner.partner_longitude, -68.06)
        self.assertEqual(self.partner.distri_location_user_id, self.driver)
        self.assertAlmostEqual(self.partner.distri_customer_latitude, -38.9512345, places=6)
        self.assertAlmostEqual(self.partner.distri_customer_longitude, -68.0598765, places=6)
        self.assertEqual(round(self.partner.distri_customer_location_accuracy), 18)
        self.assertEqual(self.partner.distri_customer_location_order_id, self.order)
        self.assertIn("google.com/maps?q=-38.951234", self.partner.distri_customer_location_url)

        route = self._route(self.order)
        customer = route._app_payload()["stops"][0]["customer"]
        self.assertAlmostEqual(customer["lat"], -38.95)
        self.assertAlmostEqual(customer["customer_lat"], -38.9512345, places=6)
        self.assertEqual(customer["customer_accuracy"], 18)

        message = self.order.message_ids.filtered(lambda m: "Datos para la entrega" in (m.body or ""))
        self.assertIn("no se cambió", message[:1].body)

    def test_location_fills_an_empty_contact(self):
        self.partner.write({"partner_latitude": 0.0, "partner_longitude": 0.0,
                            "distri_location_user_id": False, "distri_location_date": False})
        self._post({"accion": "ubicacion", "lat": "-38.9", "lng": "-68.1", "precision": "30"})
        self.assertAlmostEqual(self.partner.partner_latitude, -38.9)
        self.assertAlmostEqual(self.partner.partner_longitude, -68.1)
        self.assertFalse(self.partner.distri_location_user_id)
        self.assertTrue(self.partner.distri_location_date)

    def test_inaccurate_location_is_rejected(self):
        res = self._post({"accion": "ubicacion", "lat": "-38.9", "lng": "-68.1", "precision": "420"})
        self.assertIn("La ubicación no es precisa (±420 m)", res.text)
        for bad in ({"lat": "abc", "lng": "-68.1", "precision": "10"},
                    {"lat": "-38.9", "lng": "-68.1", "precision": "0"},
                    {"lat": "95", "lng": "-68.1", "precision": "10"},
                    {"lat": "nan", "lng": "-68.1", "precision": "10"}):
            res = self._post(dict(bad, accion="ubicacion"))
            self.assertIn("No pudimos leer tu ubicación", res.text)
        self.assertFalse(self.partner.distri_customer_location_date)
        self.assertAlmostEqual(self.partner.partner_latitude, -38.95)

    # ------------------------------------------------------------- notice

    def test_notice_is_shown_logged_and_sent_to_the_driver(self):
        before = self._outgoing()
        route = self._route(self.order)
        res = self._post({"accion": "aviso", "tipo": "other_day", "texto": "  el   jueves  "})
        self.assertTrue(res.url.split("#")[0].endswith("/pedido/%s" % self.token), res.url)
        self.assertEqual(self.order.distri_customer_notice_kind, "other_day")
        self.assertEqual(self.order.distri_customer_notice, "el jueves")
        self.assertIn("Tu aviso: «Traémelo otro día: el jueves»", res.text)
        self.assertIn("lo vamos a tener en cuenta", res.text)

        note = self.order.message_ids.filtered(lambda m: "Datos para la entrega" in (m.body or ""))
        self.assertEqual(len(note), 1)
        self.assertEqual(note.subtype_id, self.env.ref("mail.mt_note"))
        self.assertIn("Pide otra fecha", note.body)
        activity = self.order.activity_ids.filtered(
            lambda a: a.summary == datos_entrega.ACTIVITY_SUMMARY)
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity.user_id, self.seller)
        # the order stays on its route: a person decides
        self.assertIn(self.order, route.stop_ids.order_id)

        stop = route._app_payload()["stops"][0]
        self.assertEqual(stop["notice"]["kind"], "other_day")
        self.assertEqual(stop["notice"]["text"], "Traémelo otro día: el jueves")
        self.assertEqual(route.stop_ids.customer_notice, "Traémelo otro día: el jueves")

        # a second change reuses the same To-Do
        self._post({"accion": "aviso", "tipo": "other", "texto": "Tocar timbre del costado"})
        self.assertEqual(self.order.distri_customer_notice_text, "Tocar timbre del costado")
        self.assertEqual(len(self.order.activity_ids.filtered(
            lambda a: a.summary == datos_entrega.ACTIVITY_SUMMARY)), 1)

        res = self._post({"accion": "borrar_aviso"})
        self.assertFalse(self.order.distri_customer_notice_kind)
        self.assertNotIn("Tu aviso:", self._page())

        # nothing went out to the customer: no e-mail, no notification, no WhatsApp
        self.assertEqual(self._outgoing(), before)

    def test_notice_rules(self):
        res = self._post({"accion": "aviso", "tipo": "other", "texto": ""})
        self.assertIn("Contanos qué pasa", res.text)
        res = self._post({"accion": "aviso", "tipo": "cualquiera"})
        self.assertIn("Elegí qué nos querés avisar", res.text)
        self._post({"accion": "aviso", "tipo": "closes_early", "texto": "x" * 900})
        self.assertEqual(len(self.order.distri_customer_notice), datos_entrega.NOTICE_MAX)

        with patch.object(datos_entrega, "MAX_CHANGES_PER_HOUR", 1):
            res = self._post({"accion": "aviso", "tipo": "closes_early", "texto": "otra vez"})
        self.assertIn("Hiciste muchos cambios seguidos", res.text)

        # delivered: the notice form is gone, the rest stays
        route = self._route(self.order)
        self._depart(route)
        self._sync([self._event(route, "visit", self._visit(route.stop_ids))])
        page = self.url_open(self._entrega_url()).text
        self.assertNotIn("Aviso para esta entrega", page)
        self.assertIn("Días y horarios en que recibís", page)
        self.assertNotIn("Tu aviso:", self._page())
        self.assertEqual(self.order._distri_customer_set_notice("other", "tarde"),
                         "Tu pedido ya fue entregado.")
