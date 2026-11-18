"""Details found in the integral test of 27/09/2026 (prueba-integral/)."""
from datetime import datetime

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.cobros import report_generated_at
from odoo.addons.distri_pedido.tests.test_socio_generico import GenericMixin


@tagged("post_install", "-at_install", "distri_detalles")
class TestLoginLanding(HttpCase):
    """Armador and repartidor land on their screen; administration keeps Odoo."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.packer = new_test_user(cls.env, login="armador_landing", name="Armador Landing",
                                   groups="base.group_user,distri_pedido.grupo_armador")
        cls.driver = new_test_user(cls.env, login="repartidor_landing", name="Repartidor Landing",
                                   groups="base.group_user,distri_pedido.grupo_reparto")
        cls.admin = new_test_user(cls.env, login="admin_landing", name="Admin Landing",
                                  groups="base.group_user,distri_pedido.grupo_administracion")

    def _login(self, login):
        self.url_open("/web/session/logout")
        page = self.url_open("/web/login")
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
        return self.url_open("/web/login", data={"login": login, "password": login,
                                                 "csrf_token": csrf}, allow_redirects=False)

    def test_landing_by_role(self):
        self.assertEqual(self.packer._distri_landing(), "/armado")
        self.assertEqual(self.driver._distri_landing(), "/reparto")
        self.assertEqual(self.admin._distri_landing(), "")
        res = self._login("armador_landing")
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/armado"), res.headers["Location"])
        res = self._login("repartidor_landing")
        self.assertTrue(res.headers["Location"].endswith("/reparto"), res.headers["Location"])
        # the backend home of a driver goes to the app; a menu or record does not
        res = self.url_open("/odoo", allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/reparto"))
        res = self._login("admin_landing")
        self.assertIn("/odoo", res.headers["Location"])
        # 19.0.1.76.0: the bare backend home of the staff is our tablero, not Discuss
        res = self.url_open("/odoo", allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/tablero"), res.headers["Location"])
        self.assertNotIn("Location", self.url_open("/odoo?apps=1", allow_redirects=False).headers)
        page = self.url_open("/tablero")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Deudores y estado de cuenta", page.text)
        self.assertIn("Cobros", page.text)
        self.assertIn('href="/cuentas"', page.text)
        self.assertIn("Todas las aplicaciones", page.text)


@tagged("post_install", "-at_install", "distri_detalles")
class TestZonaYCiudad(GenericMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_generics()
        cls.product = cls.env["product.product"].create({
            "name": "Producto zona", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.neuquen = cls.env.ref("distri_pedido.zona_neuquen")
        cls.zapala = cls.env.ref("distri_pedido.zona_zapala")

    def _order(self, partner, **vals):
        return self.env["sale.order"].sudo().create(dict({
            "partner_id": partner.id,
            "order_line": [Command.create({"product_id": self.product.id, "product_uom_qty": 1,
                                           "price_unit": 100.0, "tax_ids": [Command.clear()]})],
        }, **vals))

    def test_customer_without_city_gets_the_zone_of_its_name(self):
        """«SIN ZONA» in /armado: a customer without a city (a made-up customer here) now takes the zone of the name prefix."""
        capital = self.env["res.partner"].create({"name": "PRUEBA SIN CIUDAD"})
        interior = self.env["res.partner"].create({"name": "Z - KIOSCO DEL CENTRO"})
        self.assertEqual(self._order(capital).distri_zona_id, self.neuquen)
        self.assertEqual(self._order(interior).distri_zona_id, self.zapala)
        # a city that is in the zone table still wins
        interior.city = "NEUQUEN"
        self.assertEqual(self._order(interior).distri_zona_id, self.neuquen)
        # a web order without customer keeps the town written in the address
        web = self._order(self.retail, distri_domicilio="San Martín 40, Zapala")
        self.assertEqual(web.distri_zona_id, self.zapala)

    def test_new_customer_gets_the_town_of_the_address(self):
        order = self._order(self.retail, distri_nombre="Almacén Nuevo",
                            distri_domicilio="Av. Argentina 120, Zapala")
        accion = order.with_user(self.admin_distri).action_distri_asignar_cliente()
        asistente = self.env[accion["res_model"]].with_user(self.admin_distri).with_context(
            accion["context"]).create({"modo": "nuevo"})
        self.assertEqual(asistente.nuevo_ciudad, "Zapala")
        asistente.action_asignar()
        self.assertEqual(order.partner_id.city, "Zapala")


@tagged("post_install", "-at_install", "distri_detalles")
class TestCierreMercadoPago(TransactionCase):

    def _report(self, generated, last_row, start="2030-01-15 03:10:00"):
        return self.env["distri.mp.report"].sudo().create({
            "name": "manual.csv", "period_start": start, "period_end": last_row,
            "generated_at": generated})

    def _day(self):
        day = self.env["distri.mp.day"].sudo().create({"date": "2030-01-15"})
        return day.with_user(self.env.ref("base.user_admin"))

    def test_generated_at_from_the_file_name(self):
        self.assertEqual(report_generated_at("settlement_v2-100000001-manual-2026-09-24-235946.csv"),
                         datetime(2026, 9, 25, 2, 59, 46))
        self.assertFalse(report_generated_at("mp_report_25col.csv"))

    def test_incomplete_report_needs_a_reason(self):
        self.env.ref("base.user_admin").sudo().write({"group_ids": [
            Command.link(self.env.ref("distri_pedido.grupo_administracion").id)]})
        # generated at 23:05 of the 15th (Argentina): the day is not covered
        self._report("2030-01-16 02:05:00", "2030-01-16 01:50:00")
        day = self._day()
        self.assertFalse(day.covered)
        with self.assertRaisesRegex(UserError, "Reporte incompleto"):
            day.action_close()
        day.incomplete_reason = "Cierre de mes: el reporte de la noche llega mañana"
        day.action_close()
        self.assertEqual(day.state, "closed")
        self.assertTrue(day.closed_incomplete)
        self.assertTrue(any("reporte incompleto" in (m.body or "") for m in day.message_ids))

    def test_report_until_23_59_covers_the_day(self):
        # a report of the whole day, generated at 23:59:46
        self._report("2030-01-16 02:59:46", "2030-01-16 01:50:00")
        self.assertTrue(self._day().covered)

    def test_report_of_the_next_day_only_does_not_cover(self):
        self._report("2030-01-17 09:00:00", "2030-01-17 02:00:00", start="2030-01-16 03:00:00")
        self.assertFalse(self._day().covered)
