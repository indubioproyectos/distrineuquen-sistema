"""19.0.1.102.0: the phone of /pedido is checked (models/ar_phone.py
check_ar_phone, the same rules as the page) and stored as +549 + 10 digits,
and the line cap of /pedido/enviar is above the whole catalog.
Synthetic data only."""
import json
from urllib.parse import urlencode

from odoo import Command
from odoo.tests import BaseCase, HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.main import (
    MAX_CANTIDAD, MAX_CUERPO, MAX_RENGLONES, MAX_RENGLONES_TEXTO,
)
from odoo.addons.distri_pedido.models.ar_phone import (
    MSG_FALTA_CARACTERISTICA, MSG_REVISAR_TELEFONO, check_ar_phone, format_ar_national,
)
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido

NOMBRE = "Kiosco Telefono V102"


@tagged("post_install", "-at_install", "distri_v102")
class TestTelefonoArgentino(BaseCase):

    def test_usual_ways_of_typing_a_mobile(self):
        for typed in ("299 4123456", "2994123456", "0299 15 4123456", "+54 9 299 412-3456",
                      "54 9 299 412 3456", "549 2994123456", "(0299) 15-412-3456",
                      "0299-154-123456", "299.412.3456", "+54 299 412 3456",
                      "54 299 15 4123456", "+54 9 299 15 412 3456", "9 299 4123456",
                      " 299 412 3456 ", "+5492994123456"):
            self.assertEqual(check_ar_phone(typed), ("2994123456", ""), typed)

    def test_other_area_codes_and_landlines(self):
        cases = {
            "11 15 4321 5678": "1143215678",
            "011 4321-5678": "1143215678",
            # the 15 only right after a real area code: never area 1115
            "11 15 1515 1234": "1115151234",
            "2942 15 423456": "2942423456",
            "02942 423456": "2942423456",
            "0351 4123456": "3514123456",
            # a landline also has 10 digits
            "299 442 1234": "2994421234",
            "+54 299 471-7396": "2994717396",
        }
        for typed, national in cases.items():
            self.assertEqual(check_ar_phone(typed), (national, ""), typed)

    def test_without_the_area_code(self):
        for typed in ("4123456", "412 3456", "15 4123456", "154123456", "412345",
                      "15-41234567", "1543215678", "41234567"):
            self.assertEqual(check_ar_phone(typed), ("", MSG_FALTA_CARACTERISTICA), typed)

    def test_typos_are_refused(self):
        for typed in ("", "  ", "abc", "41234", "12", "299 412 34567", "29941234567",
                      "299 412 345", "5412345678", "9123456789",
                      "+1 555 123 4567", "+598 99 123 456", "54 9 299 412 345",
                      "0299 15 412 345", "ENTRE MZ R LOTE 20", "299 4123456 / 299 4654321"):
            self.assertEqual(check_ar_phone(typed), ("", MSG_REVISAR_TELEFONO), typed)

    def test_shown_back_to_the_customer(self):
        self.assertEqual(format_ar_national("2994123456"), "+54 9 299 412-3456")
        self.assertEqual(format_ar_national("2942423456"), "+54 9 2942 42-3456")
        self.assertEqual(format_ar_national("1143215678"), "+54 9 11 4321-5678")

    def test_a_2000_line_order_fits_the_size_caps(self):
        self.assertGreaterEqual(MAX_RENGLONES, 2000)
        renglones = json.dumps([{"id": 100000 + i, "cantidad": MAX_CANTIDAD, "may": 1}
                                for i in range(MAX_RENGLONES)])
        self.assertLess(len(renglones), MAX_RENGLONES_TEXTO)
        cuerpo = urlencode({"renglones": renglones, "nombre": "x" * 200,
                            "telefono": "x" * 50, "domicilio": "x" * 200,
                            "comentario": "x" * 500, "csrf_token": "x" * 60})
        self.assertLess(len(cuerpo), MAX_CUERPO)


@tagged("post_install", "-at_install", "distri_v102")
class TestTelefonoYTopeEnElPedido(PedidoAbiertoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env["ir.config_parameter"].sudo()
        # many orders in a row on purpose: not the rate limit under test
        params.set_param("distri_pedido.pedido_limite_sesion_10min", "100")
        params.set_param("distri_pedido.pedido_limite_ip_10min", "100")
        Tmpl = cls.env["product.template"]
        # one unit reaches the minimum purchase (COMPRA_MINIMA)
        cls.plantilla = Tmpl.create({
            "name": "Producto Telefono V102", "list_price": 70000.0, "is_published": True,
            "sale_ok": True, "type": "consu", "taxes_id": [Command.clear()]})
        cls.muchos = Tmpl.create([{
            "name": "Producto Grande V102 %03d" % i, "list_price": 200.0, "is_published": True,
            "sale_ok": True, "type": "consu", "taxes_id": [Command.clear()]}
            for i in range(500)])
        cls.vendedor = new_test_user(cls.env, login="vendedor_v102", name="Vendedor V102",
                                     groups="base.group_user,distri_pedido.grupo_vendedor")
        # a customer whose phone was loaded long ago, without area code
        cls.cliente_viejo = cls.env["res.partner"].create({
            "name": "Almacen Telefono Viejo V102", "company_type": "company",
            "phone": "4421234"})

    def _data(self, **extra):
        data = {"renglones": json.dumps([{"id": self.plantilla.id, "cantidad": 1}]),
                "nombre": NOMBRE, "telefono": "0299 15 412-3456", "domicilio": "Calle V102 1",
                "entrega": "domicilio"}
        data.update(extra)
        return data

    def _orders(self, nombre=NOMBRE):
        return self.env["sale.order"].sudo().search([("distri_nombre", "=", nombre)])

    def test_bad_phone_is_refused_with_the_message(self):
        for telefono, mensaje in (("4123456", MSG_FALTA_CARACTERISTICA),
                                  ("299 412 34567", MSG_REVISAR_TELEFONO)):
            res = post_pedido(self, self._data(telefono=telefono))
            self.assertEqual(res.status_code, 400, telefono)
            self.assertIn(mensaje, res.text, telefono)
            # «Volver» keeps the cart
            self.assertIn("history.back()", res.text)
        self.assertFalse(self._orders())

    def test_good_phone_is_stored_normalized(self):
        res = post_pedido(self, self._data())
        self.assertEqual(res.status_code, 200)
        order = self._orders()
        self.assertEqual(len(order), 1)
        self.assertEqual(order.distri_telefono, "+5492994123456")
        self.assertIn("+54 9 299 412-3456", order.note)

    def test_the_page_checks_it_live(self):
        html = self.url_open("/pedido").text
        self.assertIn('inputmode="tel"', html)
        self.assertIn('id="o_tel"', html)
        self.assertIn("Te vamos a escribir al ", html)
        self.assertIn(MSG_FALTA_CARACTERISTICA, html)
        self.assertIn(MSG_REVISAR_TELEFONO, html)

    def test_staff_keep_the_stored_phone_of_an_old_customer(self):
        self.authenticate("vendedor_v102", "vendedor_v102")
        nombre = "Pedido Vendedor V102"
        # what the page filled in from the customer: never refused
        res = post_pedido(self, self._data(nombre=nombre, telefono="4421234",
                                           cliente_id=str(self.cliente_viejo.id)))
        self.assertEqual(res.status_code, 200)
        order = self._orders(nombre)
        self.assertEqual(order.partner_id, self.cliente_viejo)
        self.assertEqual(order.distri_telefono, "4421234")
        # what the seller types is checked
        res = post_pedido(self, self._data(nombre="Otro Vendedor V102", telefono="4654321",
                                           cliente_id=str(self.cliente_viejo.id)))
        self.assertEqual(res.status_code, 400)
        self.assertIn(MSG_FALTA_CARACTERISTICA, res.text)
        res = post_pedido(self, self._data(nombre="Otro Vendedor V102", telefono="299 465 4321",
                                           cliente_id=str(self.cliente_viejo.id)))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._orders("Otro Vendedor V102").distri_telefono, "+5492994654321")
        # without a customer the old number is just a typed one
        res = post_pedido(self, self._data(nombre="Sin Cliente V102", telefono="4421234"))
        self.assertEqual(res.status_code, 400)

    def test_a_500_line_order_is_accepted(self):
        self.assertGreater(MAX_RENGLONES, self.env["product.template"].search_count(
            [("sale_ok", "=", True), ("active", "=", True)]),
            "the line cap is above the whole catalog")
        renglones = [{"id": t.id, "cantidad": 1} for t in self.muchos]
        res = post_pedido(self, self._data(renglones=json.dumps(renglones),
                                           nombre="Pedido Grande V102"))
        self.assertEqual(res.status_code, 200)
        order = self._orders("Pedido Grande V102")
        self.assertEqual(len(order.order_line.filtered(lambda l: not l.display_type)), 500)
